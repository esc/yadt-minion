import sys
import re
import subprocess
import os
import socket
import datetime
import stat
import platform
import sh

import netifaces
import yaml
import yum
import yadtminion.yaml_merger
from yadtminion import locking
import rpm
from rpmUtils.miscutils import stringToVersion


class YumDeps(object):
    NAME_VERSION_SEPARATOR = '/'

    def __init__(self, yumbase):
        self.yumbase = yumbase
        self.requires = YumDeps._get_requires(yumbase)
        self.whatrequires = YumDeps._get_whatrequires(self.requires)
        self.all_updates = None

    @classmethod
    def get_id(clazz, pkg):
        if pkg.epoch and pkg.epoch != '0':
            return '%s%s%s:%s-%s.%s' % (pkg.name,
                                        YumDeps.NAME_VERSION_SEPARATOR,
                                        pkg.epoch,
                                        pkg.version,
                                        pkg.release,
                                        pkg.arch)
        return '%s%s%s-%s.%s' % (pkg.name,
                                 YumDeps.NAME_VERSION_SEPARATOR,
                                 pkg.version,
                                 pkg.release,
                                 pkg.arch)

    @classmethod
    def _get_requires(clazz, yumbase):
        requires = {}
        for pkg in yumbase.rpmdb.returnPackages():
            id = YumDeps.get_id(pkg)
            requires[id] = set()
            for dep in pkg.requiresList():
                if dep.startswith('rpmlib'):
                    continue
                installeds = yumbase.returnInstalledPackagesByDep(dep)
                requires[id].update(map(YumDeps.get_id, installeds))
            requires[id] = list(requires[id])
        return requires

    @classmethod
    def _get_whatrequires(clazz, requires):
        whatrequires = {}
        for pkg, requireds in requires.iteritems():
            for required in requireds:
                if required not in whatrequires:
                    whatrequires[required] = []
                whatrequires[required].append(pkg)
        return whatrequires

    def get_service_artefact(self, service_file):
        sas = self.yumbase.rpmdb.getProvides(service_file)
        if not sas:
            return None
        if len(sas) > 1:
            sys.stderr.write(
                'ERROR: %(service_file)s cannot be mapped to exactly one package: %(sas)s\n' % locals())
            return None
        return YumDeps.get_id(sas.keys()[0])

    def get_all_whatrequires(self, artefact, visited=None):
        if not visited:
            visited = set()
        if artefact in visited:
            return []
        visited.add(artefact)
        requires = self.whatrequires.get(artefact, [])
        result = [artefact]
        if artefact not in self.stop_artefacts:
            for require in requires:
                result.extend(self.get_all_whatrequires(require, visited))
        return result

    def get_all_requires(self, artefacts, visited=None):
        if not visited:
            visited = set()
        result = []
        for artefact in artefacts:
            if artefact in visited:
                continue
            visited.add(artefact)
            result.append(artefact)

            if artefact not in self.stop_artefacts:
                new_artefacts = self.requires.get(artefact)
                if not new_artefacts:
                    continue
                result.extend(self.get_all_requires(new_artefacts, visited))
        return result

    def strip_version(self, pkg):
        return pkg.split(YumDeps.NAME_VERSION_SEPARATOR, 1)[0]

    def load_all_updates(self):
        if not self.all_updates:
            self.all_updates = {}

            ups = self.yumbase._getUpdates()
            ups.doUpdates()
            ups.condenseUpdates()

            for up in ups.getUpdatesTuples():
                new_pkg = YumDeps.get_id(self.yumbase.getPackageObject(up[0]))
                try:
                    old_pkg = YumDeps.get_id(
                        self.yumbase.getPackageObject(up[1]))
                except yum.Errors.DepError:
                    old_pkg = self._convert_package_tuple_to_id(up[1])
                self.all_updates[new_pkg] = old_pkg
            for ob in ups.getObsoletesTuples():
                new_pkg = YumDeps.get_id(self.yumbase.getPackageObject(ob[0]))
                try:
                    old_pkg = YumDeps.get_id(
                        self.yumbase.getPackageObject(ob[1]))
                except yum.Errors.DepError:
                    old_pkg = self._convert_package_tuple_to_id(ob[1])
                self.all_updates[new_pkg] = old_pkg

        return self.all_updates

    def _convert_package_tuple_to_id(self, _tuple):
        epoch = _tuple[2]
        if int(epoch) == 0:
            return '%s/%s-%s.%s' % (_tuple[0], _tuple[3], _tuple[4], _tuple[1])
        else:
            return '%s/%s:%s-%s.%s' % (_tuple[0], epoch, _tuple[3], _tuple[4], _tuple[1])


class Status(object):

    def load_settings(self):
        _settings = yadtminion.yaml_merger.merge_yaml_files(
            '/etc/yadt.conf.d/')
        for key in ['settings', 'defaults', 'services']:
            value = _settings.get(key, {})
            setattr(self, key, value)

    def load_defaults_and_settings(self, only_config=False):
        if os.path.isfile('/etc/yadt.services'):
            sys.stderr.write("/etc/yadt.services is unsupported, please "
                             "migrate to /etc/yadt.conf.d : "
                             "https://github.com/yadt/yadtshell/wiki/Host-Configuration\n")
            sys.exit(1)
        self.load_settings()

        if not self.services:
            print >> sys.stderr, 'no service definitions found, skipping service handling'

        for name in self.services.keys():
            if self.services[name] is None:
                self.services[name] = {}
            if "unmanaged" in self.services[name] and self.services[name]["unmanaged"]:
                del self.services[name]

        if only_config:
            return

        self.artefacts_filter = re.compile(
            self.defaults.get('YADT_ARTEFACT_FILTER', '')).match

        self._determine_stop_artefacts()

    def determine_latest_kernel(self):
        kernels = [a.replace('/', '-').replace('kernel-uek', 'kernel')
                   for a in self.current_artefacts
                   if a.startswith(('kernel/', 'kernel-uek/'))]
        kernel_artefacts = sorted(
            map(stringToVersion, kernels),
            cmp=rpm.labelCompare, reverse=True)

        if kernel_artefacts:
            (epoch, name, version) = kernel_artefacts[0]
            return '%s/%s' % (name, version) if epoch == '0' else '%s/%s:%s' % (name, epoch, version)

    def next_artefacts_need_reboot(self):
        provides_inducing_reboot = self.settings.get('ARTEFACTS_INDUCING_REBOOT', [])
        updates = set([a.split("/", 1)[0] for a in self.next_artefacts.keys()])
        packages_inducing_reboot = []
        for provide in provides_inducing_reboot:
            packages_inducing_reboot.extend([pkg.name for pkg in self.yumbase.rpmdb.getProvides(provide).keys()])
        packages_inducing_reboot = set(packages_inducing_reboot)
        result = list(updates.intersection(packages_inducing_reboot))
        return result

    def is_sysv_service(self, service_name):
        liste = [ line.split("\t" ,1)[0].strip() for line in sh.chkconfig() ]
        return service_name in liste

    def __init__(self, only_config=False):
        self.service_defs = {}
        self.services = {}

        if only_config:
            self.load_defaults_and_settings(only_config=True)
            return

        self.yumbase = yum.YumBase()
        is_root = os.geteuid() == 0
        self.yumbase.preconf.init_plugins = is_root
        self.yumbase.preconf.errorlevel = 0
        self.yumbase.preconf.debuglevel = 0
        self.yumbase.conf.cache = not(is_root)
        if is_root:
            try:
                locking.try_to_acquire_yum_lock(self.yumbase)
            except locking.CannotAcquireYumLockException as e:
                sys.stderr.write("Could not acquire yum lock : '%s'" % str(e))
                sys.exit(1)
        self.yumdeps = YumDeps(self.yumbase)

        self.load_defaults_and_settings(only_config=False)

        self.setup_services()
        self.add_services_ignore()
        self.add_services_states()
        self.add_services_extra()

        self.handled_artefacts = [
            a for a in filter(self.artefacts_filter, self.yumdeps.requires.keys())]

        self.current_artefacts = self.yumdeps.requires.keys()

        self.next_artefacts = self.updates = {}
        self.yumdeps.load_all_updates()
        for a in filter(self.artefacts_filter, self.yumdeps.all_updates.keys()):
            self.updates[a] = self.yumdeps.all_updates[a]
        self.state = 'update_needed' if self.updates else 'uptodate'

        self.lockstate = self.get_lock_state()

        self.host = self.hostname = socket.gethostname().split('.', 1)[0]
        self.fqdn = socket.getfqdn()
        f = open('/proc/uptime', 'r')
        self.uptime = float(f.readline().split()[0])
        self.running_kernel = 'kernel/' + platform.uname()[2]
        self.latest_kernel = self.determine_latest_kernel()
        self.reboot_required_to_activate_latest_kernel = (self.running_kernel != self.latest_kernel
                                                          if self.latest_kernel else False)
        self.reboot_required_after_next_update = self.next_artefacts_need_reboot()
        if hasattr(self, 'settings') and self.settings.get('ssh_poll_max_seconds'):
            self.ssh_poll_max_seconds = self.settings.get(
                'ssh_poll_max_seconds')

        now = datetime.datetime.now()
        self.date = str(now)
        self.epoch = round(float(now.strftime('%s')))
        self.interface = {}
        for interface in netifaces.interfaces():
            if interface == 'lo':
                continue
            self.interface[interface] = []
            try:
                for link in netifaces.ifaddresses(interface)[netifaces.AF_INET]:
                    self.interface[interface].append(link['addr'])
            except Exception:
                pass
            self.interface[interface] = ' '.join(self.interface[interface])

        self.pwd = os.getcwd()

        self.structure_keys = [key for key in self.__dict__.keys()
                               if key not in ['yumbase',
                                              'yumdeps',
                                              'service_defs',
                                              'artefacts_filter']]

    @staticmethod
    def get_init_scripts_and_type(service_name):
        sysv_init_script = '/etc/init.d/%s' % service_name
        sysv_exists = os.path.exists(sysv_init_script)
        upstart_init_script = '/etc/init/%s.conf' % service_name
        upstart_override = '/etc/init/%s.override' % service_name
        upstart_exists = os.path.exists(upstart_init_script)
        override_exists = os.path.exists(upstart_override)
        # are there other locations for services?
        systemd_init_script = '/usr/lib/systemd/system/%s.service' % service_name
        # simplified for a start
        systemd_override = '/usr/lib/systemd/system/%s@.service' % service_name
        systemd_exists = os.path.exists(systemd_init_script)
        systemd_override_exists = os.path.exists(systemd_override)
        yb = yum.YumBase()
        yb.doConfigSetup(init_plugins=False)
        os_release = yb.conf.yumvar['releasever']


        if Status.is_sysv_service(service_name):
            init_type = "sysv"
        elif os_release == 6:
            if upstart_exists:
                init_type = "upstart"
            else:
                init_type = "serverside"
        elif os_release == 7:
            if systemd_exists:
                init_type = "systemd"
            else:
                init_type = "serverside"

        if init_type == "sysv":
            init_scripts = (sysv_init_script,)
        elif init_type == "upstart":
            if override_exists:
                init_scripts = (upstart_init_script, upstart_override)
            else:
                init_scripts = (upstart_init_script,)
        else:
            init_scripts = tuple()

        return init_scripts, init_type

    def get_service_init_details(self, service):
        init_scripts, init_type = self.get_init_scripts_and_type(service['name'])
        if init_scripts:
            service_artefacts = [
                self.yumdeps.get_service_artefact(init_script)
                for init_script in init_scripts]
            # Unpackaged files give None as service_artefact, filter those out.
            service_artefacts = filter(bool, service_artefacts)

            service['init_script'] = init_scripts
            service['init_type'] = init_type
        else:
            service['state_handling'] = init_type
            service_artefacts = []

        return service_artefacts

    def setup_services(self):
        for name, service in self.services.iteritems():
            service['name'] = name
            service_artefacts = self.get_service_init_details(service)
            if service_artefacts:
                service['service_artefact'] = service_artefacts
                service.setdefault('needs_artefacts', [])
                toplevel_artefacts = set()
                for artefact in service_artefacts:
                    toplevel_artefacts.update(self.yumdeps.get_all_whatrequires(artefact))
                    service['needs_artefacts'].extend(
                        map(self.yumdeps.strip_version, filter(
                            self.artefacts_filter, self.yumdeps.get_all_requires([artefact]))))
                service['toplevel_artefacts'] = list(toplevel_artefacts)

                service['needs_artefacts'].extend(map(self.yumdeps.strip_version, filter(
                    self.artefacts_filter, toplevel_artefacts)))

    def add_services_states(self):
        for service in self.services.values():
            init_script = service.get('init_script')
            if not init_script:
                continue
            cmds = ['/usr/bin/yadt-command',
                    '/usr/bin/yadt-service-status', service['name']]
            p = subprocess.Popen(cmds, stdout=open(os.devnull, 'w'))
            p.wait()
            service['state'] = p.returncode

    def get_lock_state(self):
        lock_file = os.path.join(self.defaults['YADT_LOCK_DIR'], 'host.lock')
        try:
            file = open(lock_file)
            return yaml.load(file)
        except IOError, e:
            if e.errno != 2:    # 2: No such file or directory
                sys.stderr.write(str(e) + '\n')
                sys.stderr.flush()
        return None

    def add_services_ignore(self):
        for service in self.services.values():
            ignore_file = os.path.join(
                self.defaults['YADT_LOCK_DIR'], 'ignore.%s' % service['name'])
            try:
                file = open(ignore_file)
                service['ignored'] = yaml.load(file)
            except IOError, e:
                if e.errno != 2:    # 2: No such file or directory
                    sys.stderr.write(str(e) + '\n')
                    sys.stderr.flush()

    def add_services_extra(self):
        executable = stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
        for service in self.services.values():
            extra_file = '/usr/bin/yadt-yadtminion-service-%s' % service[
                'name']
            if os.path.isfile(extra_file):
                mode = os.stat(extra_file).st_mode
                if mode & executable:
                    service['extra_script'] = extra_file
                    p = subprocess.Popen(extra_file, stdout=subprocess.PIPE)
                    stdoutdata, _ = p.communicate()
                    service['extra'] = yaml.load(stdoutdata)

    def _determine_stop_artefacts(self):
        self.yumdeps.stop_artefacts = []
        if hasattr(self, 'settings') and self.settings.get('package_handling'):
            stop_dependency_resolution_provides = self.settings[
                'package_handling']['stop_dependency_resolution']['provides']
            stop_artefacts = []
            for provides in stop_dependency_resolution_provides:
                stop_artefacts_for_provides = self.yumbase.rpmdb.getProvides(
                    provides).keys()
                stop_artefacts.extend(stop_artefacts_for_provides)
            self.yumdeps.stop_artefacts = [
                self.yumdeps.get_id(package) for package in stop_artefacts]

    def host_is_up_to_date(self):
        status = self.get_status()
        pending_updates = status['next_artefacts']
        return not pending_updates

    def get_status(self):
        return dict(filter(lambda item: item[0] in self.structure_keys, self.__dict__.iteritems()))
