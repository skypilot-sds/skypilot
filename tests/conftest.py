import pytest
import tempfile
from typing import List

# Usage: use
#   @pytest.mark.slow
# to mark a test as slow and to skip by default.
# https://docs.pytest.org/en/latest/example/simple.html#control-skipping-of-tests-according-to-command-line-option

# By default, only run generic tests and cloud-specific tests for GCP and Azure,
# due to the cloud credit limit for the development account.
#
# A "generic test" tests a generic functionality (e.g., autostop) that
# should work on any cloud we support. The cloud used for such a test
# is controlled by `--generic-cloud` (typically you do not need to set it).
#
# To only run tests for a specific cloud (as well as generic tests), use
# --aws, --gcp, --azure, or --lambda.
#
# To only run tests for managed spot (without generic tests), use --managed-spot.
all_clouds_in_smoke_tests = [
    'aws', 'gcp', 'azure', 'lambda', 'cloudflare', 'ibm', 'scp'
]
default_clouds_to_run = ['gcp', 'azure']

# Translate cloud name to pytest keyword. We need this because
# @pytest.mark.lambda is not allowed, so we use @pytest.mark.lambda_cloud
# instead.
cloud_to_pytest_keyword = {
    'aws': 'aws',
    'gcp': 'gcp',
    'azure': 'azure',
    'lambda': 'lambda_cloud',
    'cloudflare': 'cloudflare',
    'ibm': 'ibm',
    'scp': 'scp'
}


def pytest_addoption(parser):
    # tests marked as `slow` will be skipped by default, use --runslow to run
    parser.addoption('--runslow',
                     action='store_true',
                     default=False,
                     help='run slow tests.')
    for cloud in all_clouds_in_smoke_tests:
        parser.addoption(f'--{cloud}',
                         action='store_true',
                         default=False,
                         help=f'Only run {cloud.upper()} tests.')
    parser.addoption('--managed-spot',
                     action='store_true',
                     default=False,
                     help='Only run tests for managed spot.')
    parser.addoption(
        '--generic-cloud',
        type=str,
        default='gcp',
        choices=all_clouds_in_smoke_tests,
        help='Cloud to use for generic tests. If the generic cloud is '
        'not within the clouds to be run, it will be reset to the first '
        'cloud in the list of the clouds to be run.')

    parser.addoption('--terminate-on-failure',
                     action='store_true',
                     default=False,
                     help='Terminate test VMs on failure.')


def pytest_configure(config):
    config.addinivalue_line('markers', 'slow: mark test as slow to run')
    for cloud in all_clouds_in_smoke_tests:
        cloud_keyword = cloud_to_pytest_keyword[cloud]
        config.addinivalue_line(
            'markers', f'{cloud_keyword}: mark test as {cloud} specific')

    pytest.terminate_on_failure = config.getoption('--terminate-on-failure')


def _get_cloud_to_run(config) -> List[str]:
    cloud_to_run = []
    for cloud in all_clouds_in_smoke_tests:
        if config.getoption(f'--{cloud}'):
            if cloud == 'cloudflare':
                cloud_to_run.append(default_clouds_to_run[0])
            else:
                cloud_to_run.append(cloud)
    if not cloud_to_run:
        cloud_to_run = default_clouds_to_run
    return cloud_to_run


def pytest_collection_modifyitems(config, items):
    skip_marks = {}
    skip_marks['slow'] = pytest.mark.skip(reason='need --runslow option to run')
    skip_marks['managed_spot'] = pytest.mark.skip(
        reason='skipped, because --managed-spot option is set')
    for cloud in all_clouds_in_smoke_tests:
        skip_marks[cloud] = pytest.mark.skip(
            reason=f'tests for {cloud} is skipped, try setting --{cloud}')

    cloud_to_run = _get_cloud_to_run(config)
    generic_cloud = _generic_cloud(config)
    generic_cloud_keyword = cloud_to_pytest_keyword[generic_cloud]

    for item in items:
        if 'slow' in item.keywords and not config.getoption('--runslow'):
            item.add_marker(skip_marks['slow'])
        if _is_generic_test(
                item) and f'no_{generic_cloud_keyword}' in item.keywords:
            item.add_marker(skip_marks[generic_cloud])
        for cloud in all_clouds_in_smoke_tests:
            cloud_keyword = cloud_to_pytest_keyword[cloud]
            if (cloud_keyword in item.keywords and cloud not in cloud_to_run):
                # Need to check both conditions as 'gcp' is added to cloud_to_run
                # when tested for cloudflare
                if config.getoption('--cloudflare') and cloud == 'cloudflare':
                    continue
                item.add_marker(skip_marks[cloud])

        if (not 'managed_spot'
                in item.keywords) and config.getoption('--managed-spot'):
            item.add_marker(skip_marks['managed_spot'])

    # We run Lambda Cloud tests serially because Lambda Cloud rate limits its
    # launch API to one launch every 10 seconds.
    serial_mark = pytest.mark.xdist_group(name='serial_lambda_cloud')
    # Handle generic tests
    if generic_cloud == 'lambda':
        for item in items:
            if (_is_generic_test(item) and
                    'no_lambda_cloud' not in item.keywords):
                item.add_marker(serial_mark)
                # Adding the serial mark does not update the item.nodeid,
                # but item.nodeid is important for pytest.xdist_group, e.g.
                #   https://github.com/pytest-dev/pytest-xdist/blob/master/src/xdist/scheduler/loadgroup.py
                # This is a hack to update item.nodeid
                item._nodeid = f'{item.nodeid}@serial_lambda_cloud'
    # Handle Lambda Cloud specific tests
    for item in items:
        if 'lambda_cloud' in item.keywords:
            item.add_marker(serial_mark)
            item._nodeid = f'{item.nodeid}@serial_lambda_cloud'  # See comment on item.nodeid above


def _is_generic_test(item) -> bool:
    for cloud in all_clouds_in_smoke_tests:
        if cloud_to_pytest_keyword[cloud] in item.keywords:
            return False
    return True


def _generic_cloud(config) -> str:
    c = config.getoption('--generic-cloud')
    cloud_to_run = _get_cloud_to_run(config)
    if c not in cloud_to_run:
        c = cloud_to_run[0]
    return c


@pytest.fixture
def generic_cloud(request) -> str:
    return _generic_cloud(request.config)


@pytest.fixture
def enable_all_clouds(monkeypatch):
    from sky import clouds
    # Monkey-patching is required because in the test environment, no cloud is
    # enabled. The optimizer checks the environment to find enabled clouds, and
    # only generates plans within these clouds. The tests assume that all three
    # clouds are enabled, so we monkeypatch the `sky.global_user_state` module
    # to return all three clouds. We also monkeypatch `sky.check.check` so that
    # when the optimizer tries calling it to update enabled_clouds, it does not
    # raise exceptions.
    enabled_clouds = list(clouds.CLOUD_REGISTRY.values())
    monkeypatch.setattr(
        'sky.global_user_state.get_enabled_clouds',
        lambda: enabled_clouds,
    )
    monkeypatch.setattr('sky.check.check', lambda *_args, **_kwargs: None)
    config_file_backup = tempfile.NamedTemporaryFile(
        prefix='tmp_backup_config_default', delete=False)
    monkeypatch.setattr('sky.clouds.gcp.GCP_CONFIG_SKY_BACKUP_PATH',
                        config_file_backup.name)


@pytest.fixture
def aws_config_region(monkeypatch) -> str:
    from sky import skypilot_config
    region = 'us-west-2'
    if skypilot_config.loaded():
        ssh_proxy_command = skypilot_config.get_nested(
            ('aws', 'ssh_proxy_command'), None)
        if isinstance(ssh_proxy_command, dict) and ssh_proxy_command:
            region = list(ssh_proxy_command.keys())[0]
    return region
