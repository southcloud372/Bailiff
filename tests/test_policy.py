from agent_platform.gateway.policy import PermissionPolicy, PermissionRule, Principal


def test_deny_by_default() -> None:
    policy = PermissionPolicy()
    assert policy.allows(Principal(id="u1", roles=frozenset({"admin"})), "read_file") is False


def test_role_allowed() -> None:
    policy = PermissionPolicy(
        rules=[PermissionRule(tool="read_file", roles=frozenset({"reader"}))]
    )
    assert policy.allows(Principal(id="u1", roles=frozenset({"reader"})), "read_file") is True
    assert policy.allows(Principal(id="u2", roles=frozenset({"guest"})), "read_file") is False


def test_principal_allowed() -> None:
    policy = PermissionPolicy(
        rules=[PermissionRule(tool="write_file", principals=frozenset({"svc-x"}))]
    )
    assert policy.allows(Principal(id="svc-x"), "write_file") is True
    assert policy.allows(Principal(id="other"), "write_file") is False


def test_rule_scoped_to_tool() -> None:
    policy = PermissionPolicy(
        rules=[PermissionRule(tool="read_file", roles=frozenset({"reader"}))]
    )
    # 规则只对 read_file 生效，其它工具仍拒绝
    assert policy.allows(Principal(id="u1", roles=frozenset({"reader"})), "write_file") is False
