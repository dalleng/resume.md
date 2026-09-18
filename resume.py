#!/usr/bin/env python3
import argparse
import base64
import itertools
import logging
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time

import markdown

preamble = """\
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
<div id="resume">
"""

postamble = """\
</div>
</body>
</html>
"""

CHROME_GUESSES_MACOS = (
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
)

# https://stackoverflow.com/a/40674915/409879
CHROME_GUESSES_WINDOWS = (
    # Windows 10
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    # Windows 7
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    # Vista
    r"C:\Users\UserName\AppDataLocal\Google\Chrome",
    # XP
    r"C:\Documents and Settings\UserName\Local Settings\Application Data\Google\Chrome",
)

# https://unix.stackexchange.com/a/439956/20079
CHROME_GUESSES_LINUX = [
    "/".join((path, executable))
    for path, executable in itertools.product(
        (
            "/usr/local/sbin",
            "/usr/local/bin",
            "/usr/sbin",
            "/usr/bin",
            "/sbin",
            "/bin",
            "/opt/google/chrome",
        ),
        ("google-chrome", "chrome", "chromium", "chromium-browser"),
    )
]


def guess_chrome_path() -> str:
    if sys.platform == "darwin":
        guesses = CHROME_GUESSES_MACOS
    elif sys.platform == "win32":
        guesses = CHROME_GUESSES_WINDOWS
    else:
        guesses = CHROME_GUESSES_LINUX
    for guess in guesses:
        if os.path.exists(guess):
            logging.info("Found Chrome or Chromium at " + guess)
            return guess
    raise ValueError("Could not find Chrome. Please set CHROME_PATH.")


def title(md: str) -> str:
    """
    Return the contents of the first markdown heading in md, which we
    assume to be the title of the document.
    """
    for line in md.splitlines():
        if re.match("^#[^#]", line):  # starts with exactly one '#'
            return line.lstrip("#").strip()
    raise ValueError(
        "Cannot find any lines that look like markdown h1 headings to use as the title"
    )


def make_html(md: str, prefix: str = "resume") -> str:
    """
    Compile md to HTML and prepend/append preamble/postamble.

    Insert <prefix>.css if it exists.
    """
    try:
        with open(prefix + ".css") as cssfp:
            css = cssfp.read()
    except FileNotFoundError:
        print(prefix + ".css not found. Output will by unstyled.")
        css = ""
    return "".join(
        (
            preamble.format(title=title(md), css=css),
            markdown.markdown(md, extensions=["smarty", "abbr"]),
            postamble,
        )
    )


def is_complete_pdf(path: str) -> bool:
    """Return whether path looks like a completely written PDF file."""
    try:
        with open(path, "rb") as pdffp:
            if pdffp.read(5) != b"%PDF-":
                return False
            pdffp.seek(0, os.SEEK_END)
            size = pdffp.tell()
            pdffp.seek(max(0, size - 1024))
            return pdffp.read().rstrip().endswith(b"%%EOF")
    except OSError:
        return False


def terminate_process_group(process: subprocess.Popen) -> None:
    """Terminate a process and the isolated helper processes it started."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        elif process.poll() is None:
            process.terminate()
    except ProcessLookupError:
        pass


def write_pdf(html: str, prefix: str = "resume", chrome: str = "") -> None:
    """
    Write html to prefix.pdf
    """
    chrome = chrome or guess_chrome_path()
    html64 = base64.b64encode(html.encode("utf-8"))
    options = [
        "--no-sandbox",
        "--headless",
        "--print-to-pdf-no-header",
        # Keep both versions of this option for backwards compatibility
        # https://developer.chrome.com/docs/chromium/new-headless.
        "--no-pdf-header-footer",
        "--no-first-run",
        "--disable-background-networking",
        "--disable-gpu",
    ]

    # Ideally we'd use tempfile.TemporaryDirectory here. We can't because
    # attempts to delete the tmpdir fail on Windows because Chrome creates a
    # file the python process does not have permission to delete. See
    # https://github.com/puppeteer/puppeteer/issues/2778,
    # https://github.com/puppeteer/puppeteer/issues/298, and
    # https://bugs.python.org/issue26660. If we ever drop Python 3.9 support we
    # can use TemporaryDirectory with ignore_cleanup_errors=True as a context
    # manager.
    tmpdir = tempfile.mkdtemp(prefix="resume.md_")
    options.append(f"--crash-dumps-dir={tmpdir}")
    options.append(f"--user-data-dir={tmpdir}")
    temporary_pdf = os.path.join(tmpdir, "resume.pdf")
    destination_pdf = f"{prefix}.pdf"
    command = [
        chrome,
        *options,
        f"--print-to-pdf={temporary_pdf}",
        "data:text/html;base64," + html64.decode("utf-8"),
    ]

    try:
        # Chrome occasionally writes a valid PDF but fails to exit on macOS.
        # Capture its noisy diagnostics and stop only this isolated process once
        # the complete PDF has been flushed to disk.
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as chrome_log:
            process = subprocess.Popen(
                command,
                stdout=chrome_log,
                stderr=subprocess.STDOUT,
                start_new_session=os.name == "posix",
            )
            deadline = time.monotonic() + 30
            pdf_complete = False
            timed_out = False

            while process.poll() is None:
                pdf_complete = is_complete_pdf(temporary_pdf)
                if pdf_complete:
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.05)

            terminate_process_group(process)
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                returncode = process.wait()

            pdf_complete = pdf_complete or is_complete_pdf(temporary_pdf)
            chrome_log.seek(0)
            chrome_output = chrome_log.read().strip()

        if chrome_output:
            logging.debug("Chrome output:\n%s", chrome_output)

        if not pdf_complete:
            if chrome_output:
                logging.error("Chrome output:\n%s", chrome_output)
            if timed_out:
                raise subprocess.TimeoutExpired([chrome, "--headless"], 30)
            raise subprocess.CalledProcessError(
                returncode, [chrome, "--headless", "--print-to-pdf"]
            )

        os.replace(temporary_pdf, destination_pdf)
        logging.info(f"Wrote {destination_pdf}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if os.path.isdir(tmpdir):
            logging.debug(f"Could not delete {tmpdir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "file",
        help="markdown input file [resume.md]",
        default="resume.md",
        nargs="?",
    )
    parser.add_argument(
        "--no-html",
        help="Do not write html output",
        action="store_true",
    )
    parser.add_argument(
        "--no-pdf",
        help="Do not write pdf output",
        action="store_true",
    )
    parser.add_argument(
        "--chrome-path",
        help="Path to Chrome or Chromium executable",
    )
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.quiet:
        logging.basicConfig(level=logging.WARN, format="%(message)s")
    elif args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    prefix, _ = os.path.splitext(os.path.abspath(args.file))

    with open(args.file, encoding="utf-8") as mdfp:
        md = mdfp.read()
    html = make_html(md, prefix=prefix)

    if not args.no_html:
        with open(prefix + ".html", "w", encoding="utf-8") as htmlfp:
            htmlfp.write(html)
            logging.info(f"Wrote {htmlfp.name}")

    if not args.no_pdf:
        write_pdf(html, prefix=prefix, chrome=args.chrome_path)
