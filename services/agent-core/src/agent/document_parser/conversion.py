"""Bounded conversion using the application-managed LibreOffice distribution."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from agent.infrastructure.processes import launch_contained
from agent.storage import CoreDataError

CONVERSION_TIMEOUT = 90
CONVERSION_MEMORY = 1024 * 1024 * 1024
FILTERS = {'.docx': 'docx:Office Open XML Text', '.xlsx': 'xlsx:Calc MS Excel 2007 XML',
           '.pptx': 'pptx:Impress MS PowerPoint 2007 XML', '.pdf': 'pdf'}


@contextmanager
def _conversion_stage():
    temporary = tempfile.TemporaryDirectory(prefix='agent-office-')
    try:
        yield temporary.name
    finally:
        # Windows can retain a killed descendant's working-directory handle briefly.
        deadline = time.monotonic() + 5
        while True:
            try:
                temporary.cleanup()
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)


def converter_executable():
    if getattr(sys, 'frozen', False):
        runtime = Path(sys.executable).parent / 'office-runtime'
    else:
        runtime = Path(os.environ.get('AGENT_OFFICE_RUNTIME', Path(__file__).resolve().parents[5] / '.artifacts' / 'office-runtime'))
    return runtime / 'libreoffice' / 'program' / ('soffice.exe' if os.name == 'nt' else 'soffice')


def _profile(path):
    user = path / 'user'
    user.mkdir(parents=True)
    # Highest macro security, active content disabled, and document link updates NEVER.
    (user / 'registrymodifications.xcu').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">
<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
<prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>
<prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop>
<prop oor:name="DisableOLEAutomation" oor:op="fuse"><value>true</value></prop>
</item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>1</value></prop></item>
</oor:items>''', encoding='utf-8')


@contextmanager
def convert_office(path, output_suffix, *, cancel=None):
    """Yield a temporary converted document and remove all conversion state on exit."""
    if output_suffix not in FILTERS:
        raise ValueError('Unsupported Office conversion target')
    executable = converter_executable()
    if not executable.is_file():
        raise CoreDataError('rag_office_converter_unavailable', 'The application-managed LibreOffice converter is not installed.')
    with _conversion_stage() as temporary:
        stage = Path(temporary)
        profile = stage / 'profile'
        _profile(profile)
        source = stage / ('source' + Path(path).suffix.lower())
        shutil.copyfile(path, source)
        output = stage / 'output'
        output.mkdir()
        command = [str(executable), f'-env:UserInstallation={profile.as_uri()}', '--headless', '--invisible',
                   '--nologo', '--nodefault', '--norestore', '--nofirststartwizard',
                   '--convert-to', FILTERS[output_suffix], '--outdir', str(output), str(source)]
        process = job = None
        try:
            process, job = launch_contained(command, memory_limit=CONVERSION_MEMORY,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=stage)
            deadline = time.monotonic() + CONVERSION_TIMEOUT
            while True:
                if cancel is not None and cancel.is_set():
                    raise CoreDataError('rag_office_conversion_cancelled', 'Local Office conversion was cancelled.')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CoreDataError('rag_office_conversion_timeout', 'Local Office conversion exceeded 90 seconds.')
                try:
                    returncode = process.wait(timeout=min(.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            # Terminate descendants before inspecting or accepting their output.
            job.close()
            converted = output / ('source' + output_suffix)
            if returncode or not converted.is_file() or not converted.stat().st_size:
                raise CoreDataError('rag_office_conversion_failed', 'Local Office conversion failed. The file may be damaged, encrypted, or unsupported by the managed converter.')
            if converted.stat().st_size > 100 * 1024 * 1024:
                raise CoreDataError('rag_document_too_large', 'Converted Office document exceeds 100 MiB.')
            yield converted
        except OSError as error:
            raise CoreDataError('rag_office_conversion_failed', 'The application-managed LibreOffice converter could not run.') from error
        finally:
            if job:
                job.close()
            if process:
                if process.poll() is None:
                    process.kill()
                process.wait()


def convert_to_pdf(path, *, cancel=None):
    return convert_office(path, '.pdf', cancel=cancel)
