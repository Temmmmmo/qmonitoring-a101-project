import pytest

from rebar.application.engineering_example import analyze_engineering_example


@pytest.mark.parametrize('kwargs', [
    {'outer_only_repair': 'yes'},
    {'outer_only_repair': True},
    {'outer_only_repair': True, 'working_host_bytes': b'{}'},
])
def test_k09_outer_delivery_requires_explicit_host_and_identity_before_source_loading(kwargs):
    with pytest.raises(ValueError, match='Outer-only repair'):
        analyze_engineering_example('k09-typical-3-14', **kwargs)


def test_k09_outer_delivery_is_not_implicitly_applied_to_s1():
    with pytest.raises(ValueError, match='Outer-only repair'):
        analyze_engineering_example('legacy-s1-t800', outer_only_repair=True,
            working_host_bytes=b'{}', confirm_identity_xy=True)
