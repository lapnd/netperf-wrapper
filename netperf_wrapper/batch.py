## -*- coding: utf-8 -*-
##
## batch.py
##
## Author:   Toke Høiland-Jørgensen (toke@toke.dk)
## Date:     11 April 2014
## Copyright (c) 2014, Toke Høiland-Jørgensen
##
## This program is free software: you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation, either version 3 of the License, or
## (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with this program.  If not, see <http://www.gnu.org/licenses/>.

import sys, pprint, string, re, time, os, subprocess, signal, itertools

try:
    from configparser import RawConfigParser
except ImportError:
    from ConfigParser import RawConfigParser
try:
    from collections import OrderedDict
except ImportError:
    from netperf_wrapper.ordereddict import OrderedDict

from netperf_wrapper import aggregators, formatters, resultset
from netperf_wrapper.metadata import record_extended_metadata

# Python2/3 compatibility
try:
    basestring
except NameError:
    basestring=str

def new(settings):
    return BatchRunner(settings)

class BatchRunner(object):

    _INTERP_REGEX =  re.compile(r"(^|[^$])(\$\{([^}]+)\})")
    _MAX_INTERP = 1000


    def __init__(self, settings):
        self.args = OrderedDict()
        self.batches = OrderedDict()
        self.commands = OrderedDict()
        self.settings = settings
        self.killed = False
        self.interpolation_values = dict()
        self.children = []
        self.log_fd = None

        for f in settings.BATCH_FILES:
            self.read(f)


    def read(self, filename):
        parser = RawConfigParser(dict_type=OrderedDict)
        read = parser.read(filename)
        if read != [filename]:
            raise RuntimeError("Unable to read batch file: %s." % filename)

        for s in parser.sections():
            typ,nam = s.split("::")
            if typ.lower() == 'arg':
                self.args[nam.lower()] = dict(parser.items(s))
            elif typ.lower() == 'batch':
                self.batches[nam.lower()] = dict(parser.items(s))
            elif typ.lower() == 'command':
                self.commands[nam.lower()] = dict(parser.items(s))
            else:
                raise RuntimeError("Unknown section type: '%s'." % typ)

        self.expand_groups()

    def expand_groups(self):
        for obj in self.args, self.batches, self.commands:
            for name, vals in obj.items():
                # Expand inheritance
                if 'inherits' in vals:
                    if not vals['inherits'] in obj:
                        raise RuntimeError("%s inherits from non-existent parent %s." % (name, vals['inherits']))
                    obj[name] = self.inherit(obj[vals['inherits']], vals)

                # Parse boolean options
                for k,v in obj[name].items():
                    if isinstance(v, basestring) and v.lower() in ('yes', 'true', 'on'):
                        obj[name][k] = True
                    elif isinstance(v, basestring) and v.lower() in ('no', 'false', 'off'):
                        obj[name][k] = False

    def inherit(self, parent, child):
        new = parent.copy()
        new.update(child)
        if 'inherits' in parent:
            new['inherits'] = "%s, %s" % (parent['inherits'], child['inherits'])

        # Make sure children are not declared abstract.
        if 'abstract' in new:
            del new['abstract']
        return new

    def get_ivar(self, name, ivars, settings):
        if name in ivars:
            return ivars[name]
        elif hasattr(settings, name.upper()):
            return str(getattr(settings, name.upper()))
        else:
            return "$${%s}" % name

    def interpolate(self, string, ivars, settings=None):
        """Perform recursive expansion of ${vars}.

        Works by looking for a string matching the expansion syntax and
        replacing that with the value of the ivars dict corresponding to the key
        inside {}. If no key matching key is found, the expansion is escaped (by
        duplicating the $), to make sure the expansion ends. Cyclic expansions
        are protected against by capping the number of iterations."""

        if not isinstance(string, basestring):
            return string

        if settings is None:
            settings = self.settings

        ret = string
        m = self._INTERP_REGEX.search(ret)
        i = 0
        while m is not None:
            k = m.group(3)
            ret = ret.replace(m.group(2), self.get_ivar(k, ivars, settings))
            m = self._INTERP_REGEX.search(ret)
            i += 1
            if i > self._MAX_INTERP:
                raise RuntimeError("Cyclic interpolation (more than %d expansions performed)." % self._MAX_INTERP)

        return ret.replace("$$", "$")

    def apply_args(self, values, args=None, settings=None):
        new = self.interpolation_values.copy()
        if args is not None:
            new.update(args)
        new.update(values)
        for k,v in new.items():
            new[k] = self.interpolate(v, new, settings)

        return new

    def commands_for(self, batchname, arg=None, settings=None):
        if arg and not arg in self.args:
            raise RuntimeError("Can't find arg '%s' when expanding batch commands." % arg)
        if not batchname in self.batches:
            raise RuntimeError("Can't find batch '%s' to expand." % batchname)
        batch = self.batches[batchname]
        if not 'commands' in batch:
            return []
        cmdnames = [i.strip() for i in batch['commands'].split(',')]
        commands = OrderedDict()

        args = {'batch_name': batchname}
        if arg:
            args.update(self.args[arg])

        while cmdnames:
            c = cmdnames.pop(0)
            if c in commands:
                continue
            if not c in self.commands:
                raise RuntimeError("Can't find command '%s' when expanding batch command." % c)
            cmd = self.apply_args(self.commands[c], args, settings)

            # Don't include disabled commands
            if not cmd.get('enabled', True):
                continue

            # Commands can specify extra commands to run; expand those, use the
            # dictionary to prevent duplicates
            extra = [i.strip() for i in cmd.get('extra_commands', '').split(',') if i.strip()]
            cmdnames.extend(extra)
            commands[c] = cmd

        return commands.values()

    def run_command(self, command):
        cmd = command['exec'].strip()
        if command['type'] in ('pre', 'post'):
            try:
                res = subprocess.check_output(cmd, universal_newlines=True, shell=True,
                                              stderr=subprocess.STDOUT)
                self.log("%s: %s" % (cmd, res))
            except subprocess.CalledProcessError as e:
                if command.get('essential', False):
                    raise RuntimeError("Essential command '%s' failed. "
                                       "Return code: %s.\nOutput:\n %s." % (cmd, e.returncode,
                                                                            "\n ".join(e.output.splitlines())))
                else:
                    self.log("%s err(%d): %s" % (cmd, e.returncode,
                                                  "\n ".join(e.output.splitlines())))
        elif command['type'] in ('monitor',):
            proc = subprocess.Popen(cmd, universal_newlines=True, shell=True,
                                              stderr=subprocess.STDOUT)
            self.children.append((proc,command.get('kill', False)))

    def kill_children(self, force=False):
        for proc,kill in self.children:
            if kill or force:
                proc.terminate()
            else:
                proc.wait()
        self.children = []

    def run_commands(self, commands, ctype, essential_only=False):
        for c in commands:
            if c['type'] == ctype and (not essential_only
                                       or c.get('essential', False)):
                self.run_command(c)


    def gen_filename(self, settings, batch, arg, host, rep):
        filename = "batch-%s-%s-%s" % (
            settings.BATCH_NAME,
            settings.TIME.strftime("%Y-%m-%dT%H%M%S"),
            batch.get('filename_extra', "%s-%s-%s" % (arg, host, rep))
            )
        return re.sub("[^A-Za-z0-9_-]", "_", filename)


    def run_batch(self, batchname):
        if not batchname in self.batches:
            raise RuntimeError("Can't find batch '%s' to run." % batchname)
        batch = self.batches[batchname]

        # A batch declared 'abstract' is not runnable
        if batch.get('abstract', False):
            return True

        args = [i.strip() for i in batch.get('for_args', '').split(',')]
        hosts = [i.strip() for i in batch.get('for_hosts', '').split(',')]
        reps = range(1,int(batch.get('repetitions', 1))+1)
        pause = int(batch.get('pause', 0))

        for arg,host,rep in itertools.product(args, hosts, reps):
            sys.stderr.write(" arg:%s host:%s rep:%02d.\n" % (arg,host,rep))
            settings = self.settings.copy()
            settings.FORMAT = 'null'
            settings.BATCH_NAME = batchname

            expand_vars = {'repetition': "%02d" % rep}
            if arg:
                expand_vars.update(self.args[arg])
            if host:
                expand_vars['hosts'] = host
            b = self.apply_args(batch, expand_vars, settings)

            settings.load_rcvalues(b.items(), override=True)
            settings.NAME = b['test_name']
            settings.load_test()
            settings.DATA_FILENAME = self.gen_filename(settings, b, arg, host, rep)

            commands = self.commands_for(batchname, arg, settings)
            self.log_fd = open("%s.log" % settings.DATA_FILENAME, "a")

            self.run_commands(commands, 'pre')
            self.run_commands(commands, 'monitor')
            try:
                self.run_test(settings)
            except KeyboardInterrupt:
                self.run_commands(commands, 'post', essential_only=True)
                raise
            self.kill_children()
            self.run_commands(commands, 'post')
            self.log_fd.close()
            self.log_fd = None

            time.sleep(pause)


    def log(self, text):
        if self.log_fd is not None:
            self.log_fd.write(text + "\n")

    def run_test(self, settings):
        settings = settings.copy()
        settings.load_test()
        res = resultset.new(settings)
        if settings.EXTENDED_METADATA:
            record_extended_metadata(res, settings.REMOTE_METADATA)

        if not settings.HOSTS:
            raise RuntimeError("Must specify host (-H option).")

        self.agg = aggregators.new(settings)
        res = self.agg.postprocess(self.agg.aggregate(res))
        if self.killed:
            return
        res.dump_dir(os.path.dirname(settings.OUTPUT) or ".")

        formatter = formatters.new(settings)
        formatter.format([res])

    def load_input(self, settings):
        settings = settings.copy()
        results = []
        test_name = None
        for filename in settings.INPUT:
            r = resultset.load(filename)
            if test_name is not None and test_name != r.meta("NAME") and not settings.GUI:
                raise RuntimeError("Result sets must be from same test (found %s/%s)" % (test_name, r.meta("NAME")))
            test_name = r.meta("NAME")
            results.append(r)

        if settings.GUI:
            load_gui(settings)

        settings.update(results[0].meta())
        settings.load_test(informational=True)

        formatter = formatters.new(settings)
        formatter.format(results)

    def run(self):
        if self.settings.INPUT:
            return self.load_input(self.settings)
        elif self.settings.BATCH_NAMES:
            if len(self.settings.BATCH_NAMES) == 1 and self.settings.BATCH_NAMES[0] == 'ALL':
                sys.stderr.write("Running all batches.\n")
                batches = self.batches.keys()
            else:
                batches = self.settings.BATCH_NAMES
            for b in batches:
                try:
                    sys.stderr.write("Running batch '%s'.\n" % b)
                    self.run_batch(b)
                except Exception as e:
                    raise RuntimeError("Error while running batch '%s': %r." % (b, e))
            return True
        else:
            return self.run_test(self.settings)

    def kill(self):
        self.killed = True
        self.kill_children(force=True)
        try:
            self.agg.kill_runners()
        except AttributeError:
            pass

    def p(self):
        for t in 'args', 'batches', 'commands':
            print("%s:\n%s\n"% (t, pprint.pformat(getattr(self, t))))


if __name__ == "__main__":
    br = BatchRunner({'data_filename': 'testing'})
    br.read(sys.argv[1])
    br.p()
    br.run_batch('tcpfair')
    pprint.pprint(br.commands_for("tcpfair", 'codel'))
