# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Copyright © 2016, Continuum Analytics, Inc. All rights reserved.
#
# The full license is in the file LICENSE.txt, distributed with this software.
# ----------------------------------------------------------------------------
"""High-level operations on a project."""
from __future__ import absolute_import

import os
import shutil

from anaconda_project.project import Project, _COMMAND_CHOICES
from anaconda_project import prepare
from anaconda_project.local_state_file import LocalStateFile
from anaconda_project.plugins.requirement import EnvVarRequirement
from anaconda_project.plugins.requirements.conda_env import CondaEnvRequirement
from anaconda_project.plugins.requirements.download import DownloadRequirement
from anaconda_project.plugins.requirements.service import ServiceRequirement
from anaconda_project.internal.simple_status import SimpleStatus
import anaconda_project.conda_manager as conda_manager


def _project_problems_status(project, description=None):
    if len(project.problems) > 0:
        errors = []
        for problem in project.problems:
            errors.append(problem)
        if description is None:
            description = "Unable to load the project."
        return SimpleStatus(success=False, description=description, logs=[], errors=errors)
    else:
        return None


def create(directory_path, make_directory=False, name=None, icon=None):
    """Create a project skeleton in the given directory.

    Returns a Project instance even if creation fails or the directory
    doesn't exist, but in those cases the ``problems`` attribute
    of the Project will describe the problem.

    If the project.yml already exists, this simply loads it.

    This will not prepare the project (create environments, etc.),
    use the separate prepare calls if you want to do that.

    Args:
        directory_path (str): directory to contain project.yml
        make_directory (bool): True to create the directory if it doesn't exist
        name (str): Name of the new project or None to leave unset (uses directory name)
        icon (str): Icon for the new project or None to leave unset (uses no icon)

    Returns:
        a Project instance
    """
    if make_directory and not os.path.exists(directory_path):
        try:
            os.makedirs(directory_path)
        except (IOError, OSError):  # py3=IOError, py2=OSError
            # allow project.problems to report the issue
            pass

    project = Project(directory_path)

    if name is not None:
        project.project_file.set_value('name', name)
    if icon is not None:
        project.project_file.set_value('icon', icon)

    # write out the project.yml; note that this will try to create
    # the directory which we may not want... so only do it if
    # we're problem-free.
    project.project_file.use_changes_without_saving()
    if len(project.problems) == 0:
        project.project_file.save()

    return project


def set_properties(project, name=None, icon=None):
    """Set simple properties on a project.

    This doesn't support properties which require prepare()
    actions to check their effects; see other calls such as
    ``add_dependencies()`` for those.

    This will fail if project.problems is non-empty.

    Args:
        project (``Project``): the project instance
        name (str): Name of the new project or None to leave unmodified
        icon (str): Icon for the new project or None to leave unmodified

    Returns:
        a ``Status`` instance indicating success or failure
    """
    failed = _project_problems_status(project)
    if failed is not None:
        return failed

    if name is not None:
        project.project_file.set_value('name', name)

    if icon is not None:
        project.project_file.set_value('icon', icon)

    project.project_file.use_changes_without_saving()

    if len(project.problems) == 0:
        # write out the project.yml if it looks like we're safe.
        project.project_file.save()
        return SimpleStatus(success=True, description="Project properties updated.")
    else:
        # revert to previous state (after extracting project.problems)
        status = SimpleStatus(success=False,
                              description="Failed to set project properties.",
                              errors=list(project.problems))
        project.project_file.load()
        return status


def _commit_requirement_if_it_works(project, env_var_or_class, conda_environment_name=None):
    project.project_file.use_changes_without_saving()

    # See if we can perform the download
    result = prepare.prepare_without_interaction(project,
                                                 provide_whitelist=(env_var_or_class, ),
                                                 conda_environment_name=conda_environment_name)

    status = result.status_for(env_var_or_class)
    if status is None or not status:
        # reload from disk, discarding our changes because they did not work
        project.project_file.load()
    else:
        # yay!
        project.project_file.save()
    return status


def add_download(project, env_var, url):
    """Attempt to download the URL; if successful, add it as a download to the project.

    The returned ``Status`` should be a ``RequirementStatus`` for
    the download requirement if it evaluates to True (on success),
    but may be another subtype of ``Status`` on failure. A False
    status will have an ``errors`` property with a list of error
    strings.

    Args:
        project (Project): the project
        env_var (str): env var to store the local filename
        url (str): url to download

    Returns:
        ``Status`` instance
    """
    failed = _project_problems_status(project)
    if failed is not None:
        return failed
    # Modify the project file _in memory only_, do not save
    existing = project.project_file.get_value(['downloads', env_var])
    if existing is not None and isinstance(existing, dict):
        project.project_file.set_value(['downloads', env_var, 'url'], url)
    else:
        project.project_file.set_value(['downloads', env_var], url)

    return _commit_requirement_if_it_works(project, env_var)


def remove_download(project, env_var):
    """Remove file or directory referenced by ``env_var`` from file system and the project.

    The returned ``Status`` will be an instance of ``SimpleStatus``. A False
    status will have an ``errors`` property with a list of error
    strings.

    Args:
        project (Project): the project
        env_var (str): env var to store the local filename

    Returns:
        ``Status`` instance
    """
    failed = _project_problems_status(project)
    if failed is not None:
        return failed
    # Modify the project file _in memory only_, do not save
    requirement = project.find_requirements(env_var, klass=DownloadRequirement)
    if not requirement:
        return SimpleStatus(success=False, description="Download requirement: {} not found.".format(env_var))
    requirement = requirement[0]

    project.project_file.unset_value(['downloads', env_var])
    project.project_file.use_changes_without_saving()

    filepath = os.path.join(project.directory_path, requirement.filename)
    label = 'file'
    if os.path.exists(filepath):
        try:
            if os.path.isdir(filepath):
                label = 'directory'
                shutil.rmtree(filepath)
            else:
                os.unlink(filepath)
        except Exception as e:
            project.project_file.load()
            return SimpleStatus(success=False, description="Failed to remove {}: {}.".format(filepath, str(e)))
    project.project_file.save()
    return SimpleStatus(success=True, description="Removed {} '{}' from project.".format(label, requirement.filename))


def _update_environment(project, name, packages, channels, create):
    failed = _project_problems_status(project)
    if failed is not None:
        return failed

    if packages is None:
        packages = []
    if channels is None:
        channels = []

    if not create and (name is not None):
        if name not in project.conda_environments:
            problem = "Environment {} doesn't exist.".format(name)
            return SimpleStatus(success=False, description=problem)

    if name is None:
        env_dict = project.project_file.root
    else:
        env_dict = project.project_file.get_value(['environments', name])
        if env_dict is None:
            env_dict = dict()
            project.project_file.set_value(['environments', name], env_dict)

    # dependencies may be a "CommentedSeq" and we don't want to lose the comments,
    # so don't convert this thing to a regular list.
    dependencies = env_dict.get('dependencies', [])
    old_dependencies_set = set(dependencies)
    for dep in packages:
        # note: we aren't smart enough to merge deps with the same
        # package name but different versions.
        if dep not in old_dependencies_set:
            dependencies.append(dep)
    env_dict['dependencies'] = dependencies

    # channels may be a "CommentedSeq" and we don't want to lose the comments,
    # so don't convert this thing to a regular list.
    new_channels = env_dict.get('channels', [])
    old_channels_set = set(new_channels)
    for channel in channels:
        if channel not in old_channels_set:
            new_channels.append(channel)
    env_dict['channels'] = new_channels

    status = _commit_requirement_if_it_works(project, CondaEnvRequirement, conda_environment_name=name)

    return status


def add_environment(project, name, packages, channels):
    """Attempt to create the environment and add it to project.yml.

    The returned ``Status`` should be a ``RequirementStatus`` for
    the environment requirement if it evaluates to True (on success),
    but may be another subtype of ``Status`` on failure. A False
    status will have an ``errors`` property with a list of error
    strings.

    Args:
        project (Project): the project
        name (str): environment name
        packages (list of str): dependencies (with optional version info, as for conda install)
        channels (list of str): channels (as they should be passed to conda --channel)

    Returns:
        ``Status`` instance
    """
    assert name is not None
    return _update_environment(project, name, packages, channels, create=True)


def remove_environment(project, name):
    """Remove the environment from project directory and remove from project.yml.

    Returns a ``Status`` subtype (it won't be a
    ``RequirementStatus`` as with some other functions, just a
    plain status).

    Args:
        project (Project): the project
        name (str): environment name

    Returns:
        ``Status`` instance
    """
    assert name is not None
    if name == 'default':
        return SimpleStatus(success=False, description="Cannot remove default environment.")

    failed = _project_problems_status(project)
    if failed is not None:
        return failed

    if name not in project.conda_environments:
        problem = "Environment {} doesn't exist.".format(name)
        return SimpleStatus(success=False, description=problem)

    env_path = project.conda_environments[name].path(project.directory_path)
    if os.path.exists(env_path):
        try:
            shutil.rmtree(env_path)
        except Exception as e:
            problem = "Failed to remove environment {}: {}.".format(name, str(e))
            return SimpleStatus(success=False, description=problem)

    project.project_file.unset_value(['environments', name])
    project.project_file.use_changes_without_saving()
    assert project.problems == []
    project.project_file.save()
    return SimpleStatus(success=True, description="Removed environment: {}.".format(name))


def add_dependencies(project, environment, packages, channels):
    """Attempt to install dependencies then add them to project.yml.

    If the environment is None rather than an env name,
    dependencies are added in the global dependencies section (to
    all environments).

    The returned ``Status`` should be a ``RequirementStatus`` for
    the environment requirement if it evaluates to True (on success),
    but may be another subtype of ``Status`` on failure. A False
    status will have an ``errors`` property with a list of error
    strings.

    Args:
        project (Project): the project
        environment (str): environment name or None for all environments
        packages (list of str): dependencies (with optional version info, as for conda install)
        channels (list of str): channels (as they should be passed to conda --channel)

    Returns:
        ``Status`` instance
    """
    return _update_environment(project, environment, packages, channels, create=False)


# there are lots of builtin ways to do this but they wouldn't keep
# comments properly in ruamel.yaml's CommentedSeq. We don't want to
# copy or wholesale replace "items"
def _filter_inplace(predicate, items):
    i = 0
    while i < len(items):
        if predicate(items[i]):
            i += 1
        else:
            del items[i]


def remove_dependencies(project, environment, packages):
    """Attempt to remove dependencies from an environment in project.yml.

    If the environment is None rather than an env name,
    dependencies are removed from the global dependencies section
    (from all environments).

    The returned ``Status`` should be a ``RequirementStatus`` for
    the environment requirement if it evaluates to True (on success),
    but may be another subtype of ``Status`` on failure. A False
    status will have an ``errors`` property with a list of error
    strings.

    Args:
        project (Project): the project
        environment (str): environment name or None for all environments
        packages (list of str): dependencies

    Returns:
        ``Status`` instance
    """
    # This is sort of one big ugly. What we SHOULD be able to do
    # is simply remove the dependency from project.yml then re-run
    # prepare, and if the packages aren't pulled in as deps of
    # something else, they get removed. This would work if our
    # approach was to always force the env to exactly the env
    # we'd have created from scratch, given our env config.
    # But that isn't our approach right now.
    #
    # So what we do right now is remove the package from the env,
    # and then remove it from project.yml, and then see if we can
    # still prepare the project.

    failed = _project_problems_status(project)
    if failed is not None:
        return failed

    assert packages is not None
    assert len(packages) > 0

    if environment is None:
        envs = project.conda_environments.values()
    else:
        env = project.conda_environments.get(environment, None)
        if env is None:
            problem = "Environment {} doesn't exist.".format(environment)
            return SimpleStatus(success=False, description=problem)
        else:
            envs = [env]

    assert len(envs) > 0

    conda = conda_manager.new_conda_manager()

    for env in envs:
        prefix = env.path(project.directory_path)
        try:
            if os.path.isdir(prefix):
                conda.remove_packages(prefix, packages)
        except conda_manager.CondaManagerError:
            pass  # ignore errors; not all the envs will exist or have the package installed perhaps

    env_dicts = []
    for env in envs:
        env_dict = project.project_file.get_value(['environments', env.name])
        if env_dict is not None:  # it can be None for the default environment (which doesn't have to be listed)
            env_dicts.append(env_dict)
    if environment is None:
        env_dicts.append(project.project_file.root)

    assert len(env_dicts) > 0

    for env_dict in env_dicts:
        # dependencies may be a "CommentedSeq" and we don't want to lose the comments,
        # so don't convert this thing to a regular list.
        dependencies = env_dict.get('dependencies', [])
        removed_set = set(packages)
        _filter_inplace(lambda dep: dep not in removed_set, dependencies)
        env_dict['dependencies'] = dependencies

    status = _commit_requirement_if_it_works(project, CondaEnvRequirement, conda_environment_name=environment)

    return status


def add_variables(project, vars_to_add):
    """Add variables in project.yml and set their values in local project state.

    Returns a ``Status`` instance which evaluates to True on
    success and has an ``errors`` property (with a list of error
    strings) on failure.

    Args:
        project (Project): the project
        vars_to_add (list of tuple): key-value pairs

    Returns:
        ``Status`` instance
    """
    failed = _project_problems_status(project)
    if failed is not None:
        return failed

    local_state = LocalStateFile.load_for_directory(project.directory_path)
    present_vars = {req.env_var for req in project.requirements if isinstance(req, EnvVarRequirement)}
    for varname, value in vars_to_add:
        local_state.set_value(['variables', varname], value)
        if varname not in present_vars:
            project.project_file.set_value(['variables', varname], None)
    project.project_file.save()
    local_state.save()

    return SimpleStatus(success=True, description="Variables added to the project file.")


def remove_variables(project, vars_to_remove):
    """Remove variables from project.yml and unset their values in local project state.

    Returns a ``Status`` instance which evaluates to True on
    success and has an ``errors`` property (with a list of error
    strings) on failure.

    Args:
        project (Project): the project
        vars_to_remove (list of tuple): key-value pairs

    Returns:
        ``Status`` instance
    """
    failed = _project_problems_status(project)
    if failed is not None:
        return failed

    local_state = LocalStateFile.load_for_directory(project.directory_path)
    for varname in vars_to_remove:
        local_state.unset_value(['variables', varname])
        project.project_file.unset_value(['variables', varname])
    project.project_file.save()
    local_state.save()

    return SimpleStatus(success=True, description="Variables removed from the project file.")


def add_command(project, command_type, name, command):
    """Add a command to project.yml.

    Returns a ``Status`` subtype (it won't be a
    ``RequirementStatus`` as with some other functions, just a
    plain status).

    Args:
       project (Project): the project
       command_type: choice of `bokeh_app`, `notebook`, `shell` or `windows` command

    Returns:
       a ``Status`` instance
    """
    if command_type not in _COMMAND_CHOICES:
        raise ValueError("Invalid command type " + command_type + " choose from " + repr(_COMMAND_CHOICES))

    failed = _project_problems_status(project)
    if failed is not None:
        return failed

    command_dict = project.project_file.get_value(['commands', name])
    if command_dict is None:
        command_dict = dict()
        project.project_file.set_value(['commands', name], command_dict)

    command_dict[command_type] = command

    project.project_file.use_changes_without_saving()

    failed = _project_problems_status(project, "Unable to add the command.")
    if failed is not None:
        # reset, maybe someone added conflicting command line types or something
        project.project_file.load()
        return failed
    else:
        project.project_file.save()
        return SimpleStatus(success=True, description="Command added to project file.")


def remove_command(project, name):
    """Remove a command from project.yml.

    Returns a ``Status`` subtype (it won't be a
    ``RequirementStatus`` as with some other functions, just a
    plain status).

    Args:
       project (Project): the project
       name (string): name of the command to be removed

    Returns:
       a ``Status`` instance
    """
    failed = _project_problems_status(project)
    if failed is not None:
        return failed

    if name not in project.commands:
        return SimpleStatus(success=False, description="Command: '{}' not found in project file.".format(name))

    command = project.commands[name]
    if command.auto_generated:
        return SimpleStatus(success=False, description="Cannot remove auto-generated command: '{}'.".format(name))

    project.project_file.unset_value(['commands', name])
    project.project_file.use_changes_without_saving()
    assert project.problems == []
    project.project_file.save()

    return SimpleStatus(success=True, description="Command: '{}' removed from project file.".format(name))


def add_service(project, service_type, variable_name=None):
    """Add a service to project.yml.

    The returned ``Status`` should be a ``RequirementStatus`` for
    the service requirement if it evaluates to True (on success),
    but may be another subtype of ``Status`` on failure. A False
    status will have an ``errors`` property with a list of error
    strings.

    Args:
        project (Project): the project
        service_type (str): which kind of service
        variable_name (str): environment variable name (None for default)

    Returns:
        ``Status`` instance
    """
    failed = _project_problems_status(project)
    if failed is not None:
        return failed

    known_types = project.plugin_registry.list_service_types()
    found = None
    for known in known_types:
        if known.name == service_type:
            found = known
            break

    if found is None:
        return SimpleStatus(success=False,
                            description="Unable to add service.",
                            logs=[],
                            errors=["Unknown service type '%s', we know about: %s" % (service_type, ", ".join(map(
                                lambda s: s.name, known_types)))])

    if variable_name is None:
        variable_name = found.default_variable

    assert len(known_types) == 1  # when this fails, see change needed in the loop below

    requirement_already_exists = False
    existing_requirements = project.find_requirements(env_var=variable_name)
    if len(existing_requirements) > 0:
        requirement = existing_requirements[0]
        if isinstance(requirement, ServiceRequirement):
            assert requirement.service_type == service_type
            # when the above assertion fails, add the second known type besides
            # redis in test_project_ops.py::test_add_service_already_exists_with_different_type
            # and then uncomment the below code.
            # if requirement.service_type != service_type:
            #    return SimpleStatus(success=False, description="Unable to add service.", logs=[],
            #                            errors=["Service %s already exists but with type '%s'" %
            #                              (variable_name, requirement.service_type)])
            # else:
            requirement_already_exists = True
        else:
            return SimpleStatus(success=False,
                                description="Unable to add service.",
                                logs=[],
                                errors=["Variable %s is already in use." % variable_name])

    if not requirement_already_exists:
        project.project_file.set_value(['services', variable_name], service_type)

    return _commit_requirement_if_it_works(project, variable_name)


def remove_service(project, variable_name):
    """Remove a service to project.yml.

    Returns a ``Status`` instance which evaluates to True on
    success and has an ``errors`` property (with a list of error
    strings) on failure.

    Args:
        project (Project): the project
        variable_name (str): environment variable name (None for default)

    Returns:
        ``Status`` instance
    """
    failed = _project_problems_status(project)
    if failed is not None:
        return failed

    requirements = project.find_requirements(variable_name, ServiceRequirement)
    if not requirements:
        return SimpleStatus(success=False,
                            description="Service requirement referenced by '{}' not found".format(variable_name))

    project.project_file.unset_value(['services', variable_name])
    project.project_file.use_changes_without_saving()
    assert project.problems == []
    prepare.unprepare(project, whitelist=[variable_name])

    project.project_file.save()
    return SimpleStatus(success=True,
                        description="Removed service requirement referenced by '{}'".format(variable_name))
