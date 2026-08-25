import json


def _freecad_executable(tmp_path):
    executable = tmp_path / "FreeCAD 1.0" / "bin" / "FreeCADCmd.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    return executable


def test_public_doctor_reports_missing_freecad_as_structured_preflight(
    tmp_path, monkeypatch, capsys
):
    from dcc_mcp_freecad import cli
    from dcc_mcp_freecad.__version__ import __version__

    monkeypatch.delenv("DCC_MCP_FREECAD_EXECUTABLE", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))

    code = cli.main(["doctor", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert code == 10
    assert report["schema_version"] == 1
    assert report["status"] == "failed"
    assert report["dcc_type"] == "freecad"
    assert report["adapter_version"] == __version__
    assert report["core_version"]
    assert report["receipt_path"] is None
    assert report["verify"] == {
        "directly_usable": False,
        "failure_stage": "host",
        "failure_reason": "FreeCADCmd was not found",
    }
    assert report["checks"]["executable"]["success"] is False
    assert len(report["next_steps"]) == 1
    assert report["next_steps"][0]["command"]


def test_no_verb_preserves_the_existing_server_entrypoint(monkeypatch):
    from dcc_mcp_freecad import cli, server

    calls = []
    monkeypatch.setattr(server, "main", lambda: calls.append("server"))

    assert cli.main([]) == 0
    assert calls == ["server"]


def test_public_doctor_verifies_the_discovered_freecad_runtime(tmp_path, monkeypatch, capsys):
    from dcc_mcp_freecad import cli, doctor

    executable = _freecad_executable(tmp_path)
    runtime_calls = []

    def status(self, timeout_secs=30):
        runtime_calls.append((self.executable, timeout_secs))
        return {"version": "1.0.2", "python_version": "3.11.9", "ready": True}

    monkeypatch.setattr(doctor.FreecadBridge, "status", status)
    monkeypatch.setattr(doctor, "runtime_core_version", lambda: "0.20.8")

    code = cli.main(
        [
            "doctor",
            "--json",
            "--dcc-path",
            str(executable),
            "--timeout",
            "1.5",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["status"] == "ok"
    assert report["verify"] == {
        "directly_usable": True,
        "failure_stage": None,
        "failure_reason": None,
    }
    assert report["checks"]["executable"] == {
        "success": True,
        "path": str(executable.resolve()),
    }
    assert report["checks"]["core"]["success"] is True
    assert report["checks"]["configuration"]["success"] is True
    assert report["checks"]["runtime"]["freecad_version"] == "1.0.2"
    assert report["checks"]["runtime"]["python_version"] == "3.11.9"
    assert runtime_calls == [(str(executable.resolve()), 1.5)]
    assert report["next_steps"] == []


def test_doctor_rejects_legacy_freecad_before_claiming_usability(tmp_path, monkeypatch, capsys):
    from dcc_mcp_freecad import cli, doctor

    executable = _freecad_executable(tmp_path)
    monkeypatch.setattr(
        doctor.FreecadBridge,
        "status",
        lambda self, timeout_secs=30: {
            "version": "0.21.2",
            "python_version": "3.10.12",
            "ready": True,
        },
    )
    monkeypatch.setattr(doctor, "runtime_core_version", lambda: "0.20.8")

    assert cli.main(["doctor", "--json", "--dcc-path", str(executable)]) == 10

    report = json.loads(capsys.readouterr().out)
    assert report["verify"]["directly_usable"] is False
    assert report["verify"]["failure_stage"] == "host_version"
    assert "1.0 or newer" in report["verify"]["failure_reason"]
    assert report["checks"]["runtime"]["success"] is False
    assert len(report["next_steps"]) == 1
    assert report["next_steps"][0]["command"]


def test_doctor_runtime_failure_uses_verify_exit(tmp_path, monkeypatch, capsys):
    from dcc_mcp_freecad import cli, doctor
    from dcc_mcp_freecad.bridge import BridgeError

    executable = _freecad_executable(tmp_path)

    def fail_status(self, timeout_secs=30):
        raise BridgeError("FreeCAD driver did not return a result")

    monkeypatch.setattr(doctor.FreecadBridge, "status", fail_status)
    monkeypatch.setattr(doctor, "runtime_core_version", lambda: "0.20.8")

    assert cli.main(["doctor", "--json", "--dcc-path", str(executable)]) == 40

    report = json.loads(capsys.readouterr().out)
    assert report["verify"]["failure_stage"] == "runtime"
    assert "did not return a result" in report["verify"]["failure_reason"]
    assert report["checks"]["runtime"]["success"] is False


def test_doctor_fails_closed_when_runtime_does_not_report_ready(tmp_path, monkeypatch, capsys):
    from dcc_mcp_freecad import cli, doctor

    executable = _freecad_executable(tmp_path)
    monkeypatch.setattr(
        doctor.FreecadBridge,
        "status",
        lambda self, timeout_secs=30: {
            "version": "1.0.2",
            "python_version": "3.11.9",
            "ready": False,
        },
    )
    monkeypatch.setattr(doctor, "runtime_core_version", lambda: "0.20.8")

    assert cli.main(["doctor", "--json", "--dcc-path", str(executable)]) == 40

    report = json.loads(capsys.readouterr().out)
    assert report["verify"]["failure_stage"] == "runtime"
    assert report["verify"]["directly_usable"] is False


def test_doctor_rejects_old_core_before_launching_freecad(tmp_path, monkeypatch, capsys):
    from dcc_mcp_freecad import cli, doctor

    executable = _freecad_executable(tmp_path)

    def unexpected_status(self, timeout_secs=30):
        raise AssertionError("FreeCAD must not launch before Core preflight passes")

    monkeypatch.setattr(doctor.FreecadBridge, "status", unexpected_status)
    monkeypatch.setattr(doctor, "runtime_core_version", lambda: "0.19.90")

    assert cli.main(["doctor", "--json", "--dcc-path", str(executable)]) == 10

    report = json.loads(capsys.readouterr().out)
    assert report["verify"]["failure_stage"] == "core"
    assert report["checks"]["core"] == {
        "success": False,
        "version": "0.19.90",
        "minimum": "0.19.91",
    }
    assert report["next_steps"][0]["command"][-1] == "dcc-mcp-core>=0.19.91"


def test_doctor_reports_invalid_environment_configuration(tmp_path, monkeypatch, capsys):
    from dcc_mcp_freecad import cli, doctor

    executable = _freecad_executable(tmp_path)
    monkeypatch.setenv("DCC_MCP_FREECAD_MAX_TIMEOUT_SECS", "not-a-number")
    monkeypatch.setattr(doctor, "runtime_core_version", lambda: "0.20.8")

    assert cli.main(["doctor", "--json", "--dcc-path", str(executable)]) == 10

    report = json.loads(capsys.readouterr().out)
    assert report["verify"]["failure_stage"] == "configuration"
    assert report["checks"]["configuration"]["success"] is False
    assert report["next_steps"][0]["command"] == ["dcc-mcp-freecad", "doctor", "--json"]


def test_doctor_rejects_non_positive_document_limit(tmp_path, monkeypatch, capsys):
    from dcc_mcp_freecad import cli, doctor

    executable = _freecad_executable(tmp_path)
    monkeypatch.setenv("DCC_MCP_FREECAD_MAX_DOCUMENT_BYTES", "0")
    monkeypatch.setattr(
        doctor.FreecadBridge,
        "status",
        lambda self, timeout_secs=30: {
            "version": "1.0.2",
            "python_version": "3.11.9",
            "ready": True,
        },
    )
    monkeypatch.setattr(doctor, "runtime_core_version", lambda: "0.20.8")

    assert cli.main(["doctor", "--json", "--dcc-path", str(executable)]) == 10

    report = json.loads(capsys.readouterr().out)
    assert report["verify"]["failure_stage"] == "configuration"
    assert "positive" in report["verify"]["failure_reason"]


def test_doctor_rejects_invalid_server_port_before_launch(tmp_path, monkeypatch, capsys):
    from dcc_mcp_freecad import cli, doctor

    executable = _freecad_executable(tmp_path)
    monkeypatch.setenv("DCC_MCP_FREECAD_PORT", "invalid")

    def unexpected_status(self, timeout_secs=30):
        raise AssertionError("invalid port must fail before FreeCAD launch")

    monkeypatch.setattr(doctor.FreecadBridge, "status", unexpected_status)
    monkeypatch.setattr(doctor, "runtime_core_version", lambda: "0.20.8")

    assert cli.main(["doctor", "--json", "--dcc-path", str(executable)]) == 10

    report = json.loads(capsys.readouterr().out)
    assert report["verify"]["failure_stage"] == "configuration"
    assert "port" in report["verify"]["failure_reason"].lower()


def test_verify_uses_the_same_standalone_runtime_contract(tmp_path, monkeypatch, capsys):
    from dcc_mcp_freecad import cli, doctor

    executable = _freecad_executable(tmp_path)
    monkeypatch.setattr(
        doctor.FreecadBridge,
        "status",
        lambda self, timeout_secs=30: {
            "version": "1.1.0",
            "python_version": "3.11.9",
            "ready": True,
        },
    )
    monkeypatch.setattr(doctor, "runtime_core_version", lambda: "0.20.8")

    assert cli.main(["verify", "--json", "--dcc-path", str(executable)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["verb"] == "verify"
    assert report["verify"]["directly_usable"] is True


def test_doctor_rejects_invalid_probe_timeout_before_launch(tmp_path, monkeypatch, capsys):
    from dcc_mcp_freecad import cli, doctor

    executable = _freecad_executable(tmp_path)

    def unexpected_status(self, timeout_secs=30):
        raise AssertionError("invalid timeout must fail before FreeCAD launch")

    monkeypatch.setattr(doctor.FreecadBridge, "status", unexpected_status)
    monkeypatch.setattr(doctor, "runtime_core_version", lambda: "0.20.8")

    assert cli.main(["doctor", "--json", "--dcc-path", str(executable), "--timeout", "0"]) == 10

    report = json.loads(capsys.readouterr().out)
    assert report["verify"]["failure_stage"] == "configuration"
    assert "timeout" in report["verify"]["failure_reason"].lower()
