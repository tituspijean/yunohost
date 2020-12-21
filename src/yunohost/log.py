# -*- coding: utf-8 -*-

""" License

    Copyright (C) 2018 YunoHost

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program; if not, see http://www.gnu.org/licenses

"""

""" yunohost_log.py

    Manage debug logs
"""

import os
import re
import yaml
import glob
import psutil

from datetime import datetime, timedelta
from logging import FileHandler, getLogger, Formatter
from io import IOBase

from moulinette import m18n, msettings
from moulinette.core import MoulinetteError
from yunohost.utils.error import YunohostError
from yunohost.utils.packages import get_ynh_package_version
from moulinette.utils.log import getActionLogger
from moulinette.utils.filesystem import read_file, read_yaml

CATEGORIES_PATH = '/var/log/yunohost/categories/'
OPERATIONS_PATH = '/var/log/yunohost/categories/operation/'
METADATA_FILE_EXT = '.yml'
LOG_FILE_EXT = '.log'
RELATED_CATEGORIES = ['app', 'domain', 'group', 'service', 'user']

logger = getActionLogger('yunohost.log')


def log_list(limit=None, with_details=False, with_suboperations=False):
    """
    List available logs

    Keyword argument:
        limit -- Maximum number of logs
        with_details -- Include details (e.g. if the operation was a success).
        Likely to increase the command time as it needs to open and parse the
        metadata file for each log...
        with_suboperations -- Include operations that are not the "main"
        operation but are sub-operations triggered by another ongoing operation
        ... (e.g. initializing groups/permissions when installing an app)
    """

    operations = {}

    logs = filter(lambda x: x.endswith(METADATA_FILE_EXT),
                  os.listdir(OPERATIONS_PATH))
    logs = list(reversed(sorted(logs)))

    if limit is not None:
        logs = logs[:limit]

    for log in logs:

        base_filename = log[:-len(METADATA_FILE_EXT)]
        md_path = os.path.join(OPERATIONS_PATH, log)

        entry = {
            "name": base_filename,
            "path": md_path,
            "description": _get_description_from_name(base_filename),
        }

        try:
            entry["started_at"] = _get_datetime_from_name(base_filename)
        except ValueError:
            pass

        try:
            metadata = read_yaml(md_path)
        except Exception as e:
            # If we can't read the yaml for some reason, report an error and ignore this entry...
            logger.error(m18n.n('log_corrupted_md_file', md_file=md_path, error=e))
            continue

        if with_details:
            entry["success"] = metadata.get("success", "?") if metadata else "?"
            entry["parent"] = metadata.get("parent")

        if with_suboperations:
            entry["parent"] = metadata.get("parent")
            entry["suboperations"] = []
        elif metadata.get("parent") is not None:
            continue

        operations[base_filename] = entry

    # When displaying suboperations, we build a tree-like structure where
    # "suboperations" is a list of suboperations (each of them may also have a list of
    # "suboperations" suboperations etc...
    if with_suboperations:
        suboperations = [o for o in operations.values() if o["parent"] is not None]
        for suboperation in suboperations:
            parent = operations.get(suboperation["parent"])
            if not parent:
                continue
            parent["suboperations"].append(suboperation)
        operations = [o for o in operations.values() if o["parent"] is None]
    else:
        operations = [o for o in operations.values()]

    operations = list(reversed(sorted(operations, key=lambda o: o["name"])))
    # Reverse the order of log when in cli, more comfortable to read (avoid
    # unecessary scrolling)
    is_api = msettings.get('interface') == 'api'
    if not is_api:
        operations = list(reversed(operations))

    return {"operation": operations}


def log_display(path, number=None, share=False, filter_irrelevant=False, with_suboperations=False):
    """
    Display a log file enriched with metadata if any.

    If the file_name is not an absolute path, it will try to search the file in
    the unit operations log path (see OPERATIONS_PATH).

    Argument:
        file_name
        number
        share
    """

    if share:
        filter_irrelevant = True

    if filter_irrelevant:
        filters = [
            r"set [+-]x$",
            r"set [+-]o xtrace$",
            r"local \w+$",
            r"local legacy_args=.*$",
            r".*Helper used in legacy mode.*",
            r"args_array=.*$",
            r"local -A args_array$",
            r"ynh_handle_getopts_args",
            r"ynh_script_progression"
        ]
    else:
        filters = []

    def _filter_lines(lines, filters=[]):

        filters = [re.compile(f) for f in filters]
        return [l for l in lines if not any(f.search(l.strip()) for f in filters)]

    # Normalize log/metadata paths and filenames
    abs_path = path
    log_path = None
    if not path.startswith('/'):
        abs_path = os.path.join(OPERATIONS_PATH, path)

    if os.path.exists(abs_path) and not path.endswith(METADATA_FILE_EXT):
        log_path = abs_path

    if abs_path.endswith(METADATA_FILE_EXT) or abs_path.endswith(LOG_FILE_EXT):
        base_path = ''.join(os.path.splitext(abs_path)[:-1])
    else:
        base_path = abs_path
    base_filename = os.path.basename(base_path)
    md_path = base_path + METADATA_FILE_EXT
    if log_path is None:
        log_path = base_path + LOG_FILE_EXT

    if not os.path.exists(md_path) and not os.path.exists(log_path):
        raise YunohostError('log_does_exists', log=path)

    infos = {}

    # If it's a unit operation, display the name and the description
    if base_path.startswith(CATEGORIES_PATH):
        infos["description"] = _get_description_from_name(base_filename)
        infos['name'] = base_filename

    if share:
        from yunohost.utils.yunopaste import yunopaste
        content = ""
        if os.path.exists(md_path):
            content += read_file(md_path)
            content += "\n============\n\n"
        if os.path.exists(log_path):
            actual_log = read_file(log_path)
            content += "\n".join(_filter_lines(actual_log.split("\n"), filters))

        url = yunopaste(content)

        logger.info(m18n.n("log_available_on_yunopaste", url=url))
        if msettings.get('interface') == 'api':
            return {"url": url}
        else:
            return

    # Display metadata if exist
    if os.path.exists(md_path):
        try:
            metadata = read_yaml(md_path)
        except MoulinetteError as e:
            error = m18n.n('log_corrupted_md_file', md_file=md_path, error=e)
            if os.path.exists(log_path):
                logger.warning(error)
            else:
                raise YunohostError(error)
        else:
            infos['metadata_path'] = md_path
            infos['metadata'] = metadata

            if 'log_path' in metadata:
                log_path = metadata['log_path']

            if with_suboperations:

                def suboperations():
                    try:
                        log_start = _get_datetime_from_name(base_filename)
                    except ValueError:
                        return

                    for filename in os.listdir(OPERATIONS_PATH):

                        if not filename.endswith(METADATA_FILE_EXT):
                            continue

                        # We first retrict search to a ~48h time window to limit the number
                        # of .yml we look into
                        try:
                            date = _get_datetime_from_name(base_filename)
                        except ValueError:
                            continue
                        if (date < log_start) or (date > log_start + timedelta(hours=48)):
                            continue

                        try:
                            submetadata = read_yaml(os.path.join(OPERATIONS_PATH, filename))
                        except Exception:
                            continue

                        if submetadata.get("parent") == base_filename:
                            yield {
                                "name": filename[:-len(METADATA_FILE_EXT)],
                                "description": _get_description_from_name(filename[:-len(METADATA_FILE_EXT)]),
                                "success": submetadata.get("success", "?")
                            }

                metadata["suboperations"] = list(suboperations())

    # Display logs if exist
    if os.path.exists(log_path):
        from yunohost.service import _tail
        if number and filters:
            logs = _tail(log_path, int(number * 4))
        elif number:
            logs = _tail(log_path, int(number))
        else:
            logs = read_file(log_path)
        logs = _filter_lines(logs, filters)
        if number:
            logs = logs[-number:]
        infos['log_path'] = log_path
        infos['logs'] = logs

    return infos


def is_unit_operation(entities=['app', 'domain', 'group', 'service', 'user'],
                      exclude=['password'], operation_key=None):
    """
    Configure quickly a unit operation

    This decorator help you to configure the record of a unit operations.

    Argument:
    entities   A list of entity types related to the unit operation. The entity
    type is searched inside argument's names of the decorated function. If
    something match, the argument value is added as related entity. If the
    argument name is different you can specify it with a tuple
    (argname, entity_type) instead of just put the entity type.

    exclude    Remove some arguments from the context. By default, arguments
    called 'password' are removed. If an argument is an object, you need to
    exclude it or create manually the unit operation without this decorator.

    operation_key   A key to describe the unit operation log used to create the
    filename and search a translation. Please ensure that this key prefixed by
    'log_' is present in locales/en.json otherwise it won't be translatable.

    """
    def decorate(func):
        def func_wrapper(*args, **kwargs):
            op_key = operation_key
            if op_key is None:
                op_key = func.__name__

            # If the function is called directly from an other part of the code
            # and not by the moulinette framework, we need to complete kwargs
            # dictionnary with the args list.
            # Indeed, we use convention naming in this decorator and we need to
            # know name of each args (so we need to use kwargs instead of args)
            if len(args) > 0:
                from inspect import getargspec
                keys = getargspec(func).args
                if 'operation_logger' in keys:
                    keys.remove('operation_logger')
                for k, arg in enumerate(args):
                    kwargs[keys[k]] = arg
                args = ()

            # Search related entity in arguments of the decorated function
            related_to = []
            for entity in entities:
                if isinstance(entity, tuple):
                    entity_type = entity[1]
                    entity = entity[0]
                else:
                    entity_type = entity

                if entity in kwargs and kwargs[entity] is not None:
                    if isinstance(kwargs[entity], basestring):
                        related_to.append((entity_type, kwargs[entity]))
                    else:
                        for x in kwargs[entity]:
                            related_to.append((entity_type, x))

            context = kwargs.copy()

            # Exclude unappropriate data from the context
            for field in exclude:
                if field in context:
                    context.pop(field, None)
            for field, value in context.items():
                if isinstance(value, file):
                    context[field] = value.name
                elif isinstance(value, IOBase):
                    context[field] = 'IOBase'
            operation_logger = OperationLogger(op_key, related_to, args=context)

            try:
                # Start the actual function, and give the unit operation
                # in argument to let the developper start the record itself
                args = (operation_logger,) + args
                result = func(*args, **kwargs)
            except Exception as e:
                operation_logger.error(e)
                raise
            else:
                operation_logger.success()
            return result
        return func_wrapper
    return decorate


class RedactingFormatter(Formatter):

    def __init__(self, format_string, data_to_redact):
        super(RedactingFormatter, self).__init__(format_string)
        self.data_to_redact = data_to_redact

    def format(self, record):
        msg = super(RedactingFormatter, self).format(record)
        self.identify_data_to_redact(msg)
        for data in self.data_to_redact:
            msg = msg.replace(data, "**********")
        return msg

    def identify_data_to_redact(self, record):

        # Wrapping this in a try/except because we don't want this to
        # break everything in case it fails miserably for some reason :s
        try:
            # This matches stuff like db_pwd=the_secret or admin_password=other_secret
            # (the secret part being at least 3 chars to avoid catching some lines like just "db_pwd=")
            # Some names like "key" or "manifest_key" are ignored, used in helpers like ynh_app_setting_set or ynh_read_manifest
            match = re.search(r'(pwd|pass|password|secret|\w+key|token)=(\S{3,})$', record.strip())
            if match and match.group(2) not in self.data_to_redact and match.group(1) not in ["key", "manifest_key"]:
                self.data_to_redact.append(match.group(2))
        except Exception as e:
            logger.warning("Failed to parse line to try to identify data to redact ... : %s" % e)


class OperationLogger(object):

    """
    Instances of this class represents unit operation done on the ynh instance.

    Each time an action of the yunohost cli/api change the system, one or
    several unit operations should be registered.

    This class record logs and metadata like context or start time/end time.
    """

    _instances = []

    def __init__(self, operation, related_to=None, **kwargs):
        # TODO add a way to not save password on app installation
        self.operation = operation
        self.related_to = related_to
        self.extra = kwargs
        self.started_at = None
        self.ended_at = None
        self.logger = None
        self._name = None
        self.data_to_redact = []
        self.parent = self.parent_logger()
        self._instances.append(self)

        for filename in ["/etc/yunohost/mysql", "/etc/yunohost/psql"]:
            if os.path.exists(filename):
                self.data_to_redact.append(read_file(filename).strip())

        self.path = OPERATIONS_PATH

        if not os.path.exists(self.path):
            os.makedirs(self.path)

    def parent_logger(self):

        # If there are other operation logger instances
        for instance in reversed(self._instances):
            # Is one of these operation logger started but not yet done ?
            if instance.started_at is not None and instance.ended_at is None:
                # We are a child of the first one we found
                return instance.name

        # If no lock exists, we are probably in tests or yunohost is used as a
        # lib ... let's not really care about that case and assume we're the
        # root logger then.
        if not os.path.exists("/var/run/moulinette_yunohost.lock"):
            return None

        locks = read_file("/var/run/moulinette_yunohost.lock").strip().split("\n")
        # If we're the process with the lock, we're the root logger
        if locks == [] or str(os.getpid()) in locks:
            return None

        # If we get here, we are in a yunohost command called by a yunohost
        # (maybe indirectly from an app script for example...)
        #
        # The strategy is :
        # 1. list 20 most recent log files
        # 2. iterate over the PID of parent processes
        # 3. see if parent process has some log file open (being actively
        # written in)
        # 4. if among those file, there's an operation log file, we use the id
        # of the most recent file

        recent_operation_logs = sorted(glob.iglob(OPERATIONS_PATH + "*.log"), key=os.path.getctime, reverse=True)[:20]

        proc = psutil.Process().parent()
        while proc is not None:
            # We use proc.open_files() to list files opened / actively used by this proc
            # We only keep files matching a recent yunohost operation log
            active_logs = sorted([f.path for f in proc.open_files() if f.path in recent_operation_logs], key=os.path.getctime, reverse=True)
            if active_logs != []:
                # extra the log if from the full path
                return os.path.basename(active_logs[0])[:-4]
            else:
                proc = proc.parent()
                continue

        # If nothing found, assume we're the root operation logger
        return None

    def start(self):
        """
        Start to record logs that change the system
        Until this start method is run, no unit operation will be registered.
        """

        if self.started_at is None:
            self.started_at = datetime.utcnow()
            self.flush()
            self._register_log()

    @property
    def md_path(self):
        """
        Metadata path file
        """
        return os.path.join(self.path, self.name + METADATA_FILE_EXT)

    @property
    def log_path(self):
        """
        Log path file
        """
        return os.path.join(self.path, self.name + LOG_FILE_EXT)

    def _register_log(self):
        """
        Register log with a handler connected on log system
        """

        self.file_handler = FileHandler(self.log_path)
        # We use a custom formatter that's able to redact all stuff in self.data_to_redact
        # N.B. : the subtle thing here is that the class will remember a pointer to the list,
        # so we can directly append stuff to self.data_to_redact and that'll be automatically
        # propagated to the RedactingFormatter
        self.file_handler.formatter = RedactingFormatter('%(asctime)s: %(levelname)s - %(message)s', self.data_to_redact)

        # Listen to the root logger
        self.logger = getLogger('yunohost')
        self.logger.addHandler(self.file_handler)

    def flush(self):
        """
        Write or rewrite the metadata file with all metadata known
        """

        dump = yaml.safe_dump(self.metadata, default_flow_style=False)
        for data in self.data_to_redact:
            # N.B. : we need quotes here, otherwise yaml isn't happy about loading the yml later
            dump = dump.replace(data, "'**********'")
        with open(self.md_path, 'w') as outfile:
            outfile.write(dump)

    @property
    def name(self):
        """
        Name of the operation
        This name is used as filename, so don't use space
        """
        if self._name is not None:
            return self._name

        name = [self.started_at.strftime("%Y%m%d-%H%M%S")]
        name += [self.operation]

        if hasattr(self, "name_parameter_override"):
            # This is for special cases where the operation is not really
            # unitary. For instance, the regen conf cannot be logged "per
            # service" because of the way it's built
            name.append(self.name_parameter_override)
        elif self.related_to:
            # We use the name of the first related thing
            name.append(self.related_to[0][1])

        self._name = '-'.join(name)
        return self._name

    @property
    def metadata(self):
        """
        Dictionnary of all metadata collected
        """

        data = {
            'started_at': self.started_at,
            'operation': self.operation,
            'parent': self.parent,
            'yunohost_version': get_ynh_package_version("yunohost")["version"],
            'interface': msettings.get('interface'),
        }
        if self.related_to is not None:
            data['related_to'] = self.related_to
        if self.ended_at is not None:
            data['ended_at'] = self.ended_at
            data['success'] = self._success
            if self.error is not None:
                data['error'] = self._error
        # TODO: detect if 'extra' erase some key of 'data'
        data.update(self.extra)
        return data

    def success(self):
        """
        Declare the success end of the unit operation
        """
        self.close()

    def error(self, error):
        """
        Declare the failure of the unit operation
        """
        return self.close(error)

    def close(self, error=None):
        """
        Close properly the unit operation
        """
        if self.ended_at is not None or self.started_at is None:
            return
        if error is not None and not isinstance(error, basestring):
            error = str(error)
        self.ended_at = datetime.utcnow()
        self._error = error
        self._success = error is None

        if self.logger is not None:
            self.logger.removeHandler(self.file_handler)
            self.file_handler.close()

        is_api = msettings.get('interface') == 'api'
        desc = _get_description_from_name(self.name)
        if error is None:
            if is_api:
                msg = m18n.n('log_link_to_log', name=self.name, desc=desc)
            else:
                msg = m18n.n('log_help_to_get_log', name=self.name, desc=desc)
            logger.debug(msg)
        else:
            if is_api:
                msg = "<strong>" + m18n.n('log_link_to_failed_log',
                                          name=self.name, desc=desc) + "</strong>"
            else:
                msg = m18n.n('log_help_to_get_failed_log', name=self.name,
                             desc=desc)
            logger.info(msg)
        self.flush()
        return msg

    def __del__(self):
        """
        Try to close the unit operation, if it's missing.
        The missing of the message below could help to see an electrical
        shortage.
        """
        if self.ended_at is not None or self.started_at is None:
            return
        else:
            self.error(m18n.n('log_operation_unit_unclosed_properly'))


def _get_datetime_from_name(name):

    # Filenames are expected to follow the format:
    # 20200831-170740-short_description-and-stuff

    raw_datetime = " ".join(name.split("-")[:2])
    return datetime.strptime(raw_datetime, "%Y%m%d %H%M%S")


def _get_description_from_name(name):
    """
    Return the translated description from the filename
    """

    parts = name.split("-", 3)
    try:
        try:
            datetime.strptime(" ".join(parts[:2]), "%Y%m%d %H%M%S")
        except ValueError:
            key = "log_" + parts[0]
            args = parts[1:]
        else:
            key = "log_" + parts[2]
            args = parts[3:]
        return m18n.n(key, *args)
    except IndexError:
        return name
