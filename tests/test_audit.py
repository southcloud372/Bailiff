from agent_platform.gateway.audit import AuditEvent, AuditLogger


def test_audit_logger_records() -> None:
    logger = AuditLogger()
    logger.log(
        AuditEvent(
            principal_id="u1",
            tool="read_file",
            arguments={"path": "a.txt"},
            outcome="allowed",
        )
    )
    logger.log(
        AuditEvent(principal_id="u2", tool="write_file", arguments={}, outcome="denied")
    )
    assert len(logger.events) == 2
    assert logger.events[0].principal_id == "u1"
    assert logger.events[1].outcome == "denied"


def test_audit_sink_called() -> None:
    seen: list[AuditEvent] = []
    logger = AuditLogger(sink=seen.append)
    logger.log(AuditEvent(principal_id="u1", tool="t", arguments={}, outcome="error"))
    assert len(seen) == 1
    assert seen[0].outcome == "error"
