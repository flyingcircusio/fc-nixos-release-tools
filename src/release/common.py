"""Common utilities used by multiple modules to avoid circular imports."""

import asyncio
import asyncio.subprocess
import io
import shlex
from pathlib import Path

WORK_DIR = Path("work")


# https://stackoverflow.com/questions/65649412/getting-live-output-from-asyncio-subprocess


async def _read_stream(stream, cb):
    while True:
        line = await stream.readline()
        if line:
            cb(line)
        else:
            break


async def _stream_subprocess(cmd, output, **kw):
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # bufsize=1,
        **kw,
    )

    await asyncio.gather(
        _read_stream(process.stdout, output.receive_stdout),
        _read_stream(process.stderr, output.receive_stderr),
    )
    return await process.wait()


class Output:
    def __init__(self, log):
        self.log = log
        self.joined = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def receive_stdout(self, line):
        line = line.decode("utf-8")
        self.log.write(line)
        self.joined.write(line)
        self.stdout.write(line)

    def receive_stderr(self, line):
        line = line.decode("utf-8")
        self.log.write(line)
        self.joined.write(line)
        self.stderr.write(line)


def execute(cmd, **kw):
    with open("commands.log", "a") as log:
        output = Output(log)
        cmd_repr = shlex.join(cmd)
        output.log.write(f"$ {cmd_repr}\n")
        rc = asyncio.run(_stream_subprocess(cmd, output, **kw))
        return rc, output
