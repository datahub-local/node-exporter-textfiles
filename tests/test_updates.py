import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location('updates', pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'updates.py')
module = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(module)
except FileNotFoundError:
    raise AssertionError('updates.py is missing')


def test_parse_apt_upgrade_output():
    sample = '''
Inst libexpat1 [2.7.1-2] (2.8.2-1~deb13u1 Debian-Security:13/stable-security [arm64])
Inst armbian-firmware [26.08.0-trunk-dietpi1] (26.08.0-trunk-dietpi2 DietPi:all [all])
Inst linux-cpupower [6.12.96-1] (6.12.100-1 Debian-Security:13/stable-security [arm64])
Conf libexpat1 (2.8.2-1~deb13u1 Debian-Security:13/stable-security [arm64])
'''
    total, security = module.parse_apt_upgrade_output(sample)
    assert total == 3
    assert security == 2


def test_conf_and_remv_lines_are_ignored():
    sample = '''
Conf libexpat1 (2.8.2-1~deb13u1 Debian-Security:13/stable-security [arm64])
Remv obsolete-pkg [1.0-1]
'''
    assert module.parse_apt_upgrade_output(sample) == (0, 0)


def test_package_pulled_in_fresh_has_no_installed_version():
    # dist-upgrade lists kernel ABI bumps without a [current version] group.
    sample = 'Inst linux-image-6.12 (6.12.100-1 Debian-Security:13/stable-security [arm64])'
    assert module.parse_apt_upgrade_output(sample) == (1, 1)


def test_multiple_origins_and_trailing_group():
    sample = 'Inst somepkg [1.0] (2.0 Debian:13/stable, Debian-Security:13/stable-security [amd64]) []'
    assert module.parse_apt_upgrade_output(sample) == (1, 1)


def test_non_debian_origins_are_not_security():
    # These nodes carry DietPi and Armbian repos next to Debian's.
    sample = 'Inst armbian-firmware [26.08.0-trunk-dietpi1] (26.08.0-trunk-dietpi2 DietPi:all [all])'
    assert module.parse_apt_upgrade_output(sample) == (1, 0)


def test_duplicate_package_counted_once():
    sample = '''
Inst foo [1] (2 Debian:13/stable [amd64])
Inst foo [1] (2 Debian-Security:13/stable-security [amd64])
'''
    assert module.parse_apt_upgrade_output(sample) == (1, 1)


def test_garbage_and_empty_input_yield_zero():
    assert module.parse_apt_upgrade_output('') == (0, 0)
    assert module.parse_apt_upgrade_output('sh: apt-get: not found') == (0, 0)


def test_summary_line_gate_distinguishes_zero_from_garbage():
    # A real "nothing pending" run always carries the summary line; output
    # without it must not be reported as 0 pending.
    assert module.SUMMARY_RE.search('0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.')
    assert not module.SUMMARY_RE.search('E: Could not open lock file')


def test_host_probe_parsing():
    probe = module.parse_host_probe(
        'pretty_name=Debian GNU/Linux 13 (trixie)\nreboot_required=1\ncache_timestamp=1755800000\n'
    )
    assert probe['pretty_name'] == 'Debian GNU/Linux 13 (trixie)'
    assert probe['reboot_required'] == '1'
    assert probe['cache_timestamp'] == '1755800000'


def test_render_emits_required_metric_names_with_help_and_type():
    out = module.render({
        'upgrades_pending': 7,
        'security_upgrades_pending': 3,
        'reboot_required': 1,
        'package_cache_timestamp': 1755800000.0,
    })
    assert '# HELP node_apt_upgrades_pending Apt packages pending upgrade.' in out
    assert '# TYPE node_apt_upgrades_pending gauge' in out
    assert 'node_apt_upgrades_pending 7.0' in out
    assert 'node_apt_security_upgrades_pending 3.0' in out
    assert 'node_reboot_required 1.0' in out
    assert 'node_apt_package_cache_timestamp_seconds' in out


def test_render_omits_cache_timestamp_when_unavailable():
    out = module.render({
        'upgrades_pending': 0,
        'security_upgrades_pending': 0,
        'reboot_required': 0,
    })
    assert 'node_apt_package_cache_timestamp_seconds' not in out
    assert 'node_apt_upgrades_pending 0.0' in out
