# Copyright 2020 Red Hat, Inc.
# Author: Jake Hunsaker <jhunsake@redhat.com>

# This file is part of the sos project: https://github.com/sosreport/sos
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# version 2 of the GNU General Public License.
#
# See the LICENSE file in the source distribution for further information.

import json
import logging
import os
import tempfile
import sys

from argparse import SUPPRESS
from datetime import datetime
from shutil import rmtree
from pathlib import Path
from sos import __version__
from sos.archive import TarFileArchive
from sos.options import SoSOptions
from sos.utilities import TempFileUtil


class SoSComponent():
    """Any sub-command that sos supports needs to subclass SoSComponent in
    order to be properly supported by the sos binary.

    This class contains the standardized entrypoint for subcommands, as well as
    building out supported options from both globally shared option lists, and
    options supported by that specific subcommand.

    When sos initializes, it will load an unintialized instance of each class
    found within one recursion of the module root directory that subclasses
    SoSComponent.

    If sos is able to match the user-specified subcommand to one that exists
    locally, then that SoSComponent is initialized, logging is setup, and a
    policy is loaded. From there, the component's execute() method takes over.

    Added in 4.0
    """

    desc = 'unset'

    arg_defaults = {}
    configure_logging = True
    load_policy = True
    root_required = False

    _arg_defaults = {
        "batch": False,
        "compression_type": 'auto',
        "config_file": '/etc/sos/sos.conf',
        "debug": False,
        "encrypt_key": None,
        "encrypt_pass": None,
        "quiet": False,
        "threads": 4,
        "tmp_dir": '',
        "sysroot": None,
        "verbosity": 0
    }

    def __init__(self, parser, parsed_args, cmdline_args):
        self.parser = parser
        self.args = parsed_args
        self.cmdline = cmdline_args
        self.exit_process = False
        self.archive = None
        self.tmpdir = None
        self.tempfile_util = None
        self.manifest = None

        try:
            import signal
            signal.signal(signal.SIGTERM, self.get_exit_handler())
        except Exception:
            pass

        # update args from component's arg_defaults defintion
        self._arg_defaults.update(self.arg_defaults)
        self.opts = self.load_options()

        if self.configure_logging:
            tmpdir = self.get_tmpdir_default()
            # only setup metadata if we are logging
            self.manifest = SoSMetadata()

            if not os.path.isdir(tmpdir) \
                    or not os.access(tmpdir, os.W_OK):
                msg = "temporary directory %s " % tmpdir
                msg += "does not exist or is not writable\n"
                # write directly to stderr as logging is not initialised yet
                sys.stderr.write(msg)
                self._exit(1)

            self.sys_tmp = tmpdir

            self.tmpdir = tempfile.mkdtemp(prefix="sos.", dir=self.sys_tmp)
            self.tempfile_util = TempFileUtil(self.tmpdir)
            self._setup_logging()

        if self.load_policy:
            try:
                import sos.policies
                self.policy = sos.policies.load(sysroot=self.opts.sysroot)
            except KeyboardInterrupt:
                self._exit(0)
            self._is_root = self.policy.is_root()

        if self.manifest is not None:
            self.manifest.add_field('version', __version__)
            self.manifest.add_field('cmdline', ' '.join(self.cmdline))
            self.manifest.add_field('start_time', datetime.now())
            # these three will be set later, add here for organization
            self.manifest.add_field('end_time', '')
            self.manifest.add_field('run_time', '')
            self.manifest.add_field('compression', '')
            self.manifest.add_field('policy', self.policy.distro)
            self.manifest.add_section('components')

    def execute(self):
        raise NotImplementedError

    def get_exit_handler(self):
        def exit_handler(signum, frame):
            self.exit_process = True
            self._exit()
        return exit_handler

    def _exit(self, error=0):
        raise SystemExit(error)

    def get_tmpdir_default(self):
        """If --tmp-dir is not specified, provide a default location.
        Normally this is /var/tmp, but if we detect we are in a container, then
        use a standardized env var to redirect to the host's filesystem instead
        """
        if self.opts.tmp_dir:
            return os.path.abspath(self.opts.tmp_dir)

        tmpdir = '/var/tmp'

        if os.getenv('HOST', None) and os.getenv('container', None):
            tmpdir = os.path.join(os.getenv('HOST'), tmpdir.lstrip('/'))

        return tmpdir

    def check_listing_options(self):
        opts = [o for o in self.opts.dict().keys() if o.startswith('list')]
        if opts:
            return any([getattr(self.opts, opt) for opt in opts])

    @classmethod
    def add_parser_options(cls, parser):
        """This should be overridden by each subcommand to add its own unique
        options to the parser
        """
        pass

    def apply_options_from_cmdline(self, opts):
        """(Re-)apply options specified via the cmdline to an options instance

        There are several cases where we may need to re-apply the options from
        the cmdline over previously loaded options - for instance when an
        option is specified in both a config file and cmdline, or a preset and
        the cmdline, or all three.

        Use this to re-apply cmdline option overrides to anything that may
        change the default values of options

        Positional arguments:

            :param opts:        SoSOptions object to update

        """
        # override the values from config files with the values from the
        # cmdline iff that value was explicitly specified, and compare it to
        # the _current_ set of opts from the config files as a default
        cmdopts = SoSOptions().from_args(
            self.parser.parse_args(self.cmdline),
            arg_defaults=opts.dict(preset_filter=False)
        )
        # we can't use merge() here, as argparse will pass default values for
        # unset options which would effectively negate config file settings and
        # set all values back to their normal default
        codict = cmdopts.dict(preset_filter=False)
        for opt, val in codict.items():
            if opt not in cmdopts.arg_defaults.keys():
                continue
            if val and val != opts.arg_defaults[opt]:
                setattr(opts, opt, val)

        return opts

    def load_options(self):
        """Compile arguments loaded from defaults, config files, and the
        command line into a usable set of options
        """
        # load the defaults defined by the component and the shared options
        opts = SoSOptions(arg_defaults=self._arg_defaults)

        for option in self.parser._actions:
            if option.default != SUPPRESS:
                option.default = None

        opts.update_from_conf(self.args.config_file, self.args.component)
        if os.getuid() != 0:
            userconf = os.path.join(Path.home(), '.config/sos/sos.conf')
            if os.path.exists(userconf):
                opts.update_from_conf(userconf, self.args.component)

        opts = self.apply_options_from_cmdline(opts)

        return opts

    def cleanup(self):
        # archive and tempfile cleanup may fail due to a fatal
        # OSError exception (ENOSPC, EROFS etc.).
        try:
            if self.archive:
                self.archive.cleanup()
            if self.tempfile_util:
                self.tempfile_util.clean()
            if self.tmpdir:
                rmtree(self.tmpdir)
        except Exception as err:
            print("Failed to finish cleanup: %s\nContents may remain in %s"
                  % (err, self.tmpdir))

    def setup_archive(self, name=''):
        enc_opts = {
            'encrypt': True if (self.opts.encrypt_pass or
                                self.opts.encrypt_key) else False,
            'key': self.opts.encrypt_key,
            'password': self.opts.encrypt_pass
        }
        if not name:
            name = self.policy.get_archive_name()
        archive_name = os.path.join(self.tmpdir, name)
        if self.opts.compression_type == 'auto':
            auto_archive = self.policy.get_preferred_archive()
            self.archive = auto_archive(archive_name, self.tmpdir,
                                        self.policy, self.opts.threads,
                                        enc_opts, self.opts.sysroot,
                                        self.manifest)

        else:
            self.archive = TarFileArchive(archive_name, self.tmpdir,
                                          self.policy, self.opts.threads,
                                          enc_opts, self.opts.sysroot,
                                          self.manifest)

        self.archive.set_debug(True if self.opts.debug else False)

    def _setup_logging(self):
        """Creates the log handler that shall be used by all components and any
        and all related bits to those components that need to log either to the
        console or to the log file for that run of sos.
        """
        # main soslog
        self.soslog = logging.getLogger('sos')
        self.soslog.setLevel(logging.DEBUG)
        flog = None
        if not self.check_listing_options():
            self.sos_log_file = self.get_temp_file()
            flog = logging.StreamHandler(self.sos_log_file)
            flog.setFormatter(logging.Formatter(
                '%(asctime)s %(levelname)s: %(message)s'))
            flog.setLevel(logging.INFO)
            self.soslog.addHandler(flog)

        if not self.opts.quiet:
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(logging.Formatter('%(message)s'))
            if self.opts.verbosity and self.opts.verbosity > 1:
                console.setLevel(logging.DEBUG)
                if flog:
                    flog.setLevel(logging.DEBUG)
            elif self.opts.verbosity and self.opts.verbosity > 0:
                console.setLevel(logging.INFO)
                if flog:
                    flog.setLevel(logging.DEBUG)
            else:
                console.setLevel(logging.WARNING)
            self.soslog.addHandler(console)
        # still log ERROR level message to console, but only setup this handler
        # when --quiet is used, as otherwise we'll double log
        else:
            console_err = logging.StreamHandler(sys.stderr)
            console_err.setFormatter(logging.Formatter('%(message)s'))
            console_err.setLevel(logging.ERROR)
            self.soslog.addHandler(console_err)

        # ui log
        self.ui_log = logging.getLogger('sos_ui')
        self.ui_log.setLevel(logging.INFO)
        if not self.check_listing_options():
            self.sos_ui_log_file = self.get_temp_file()
            ui_fhandler = logging.StreamHandler(self.sos_ui_log_file)
            ui_fhandler.setFormatter(logging.Formatter(
                '%(asctime)s %(levelname)s: %(message)s'))

            self.ui_log.addHandler(ui_fhandler)

        if not self.opts.quiet:
            ui_console = logging.StreamHandler(sys.stdout)
            ui_console.setFormatter(logging.Formatter('%(message)s'))
            ui_console.setLevel(logging.INFO)
            self.ui_log.addHandler(ui_console)

    def get_temp_file(self):
        return self.tempfile_util.new()


class SoSMetadata():
    """This class is used to record metadata from a sos execution that will
    then be stored as a JSON-formatted manifest within the final tarball.

    It can be extended by adding further instances of SoSMetadata to represent
    dict-like structures throughout the various sos bits that record to
    metadata
    """

    def add_field(self, field_name, content):
        """Add a key, value entry to the current metadata instance
        """
        setattr(self, field_name, content)

    def add_section(self, section_name):
        """Adds a new instance of SoSMetadata to the current instance
        """
        setattr(self, section_name, SoSMetadata())
        return getattr(self, section_name)

    def add_list(self, list_name, content=[]):
        """Add a named list element to the current instance. If content is not
        supplied, then add an empty list that can alter be appended to
        """
        if not isinstance(content, list):
            raise TypeError('content added must be list')
        setattr(self, list_name, content)

    def get_json(self, indent=None):
        """Convert contents of this SoSMetdata instance, and all other nested
        instances (sections), into a json-formatted output.

        Used to write manifest.json to the final archives.
        """
        return json.dumps(self,
                          default=lambda o: getattr(o, '__dict__', str(o)),
                          indent=indent)

# vim: set et ts=4 sw=4 :
