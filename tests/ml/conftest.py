"""All fixtures are generated under pytest's temporary directory, never private_data."""
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def make_video(tmp_path):
    def make(name: str = "X1.mov", *, variable_fps: bool = False) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        command = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                   "testsrc=size=96x64:rate=10:duration=2", "-an"]
        if variable_fps:
            command += ["-vf", "settb=1/1000,setpts='if(lt(N,10),N*100,1000+(N-10)*200)'", "-fps_mode", "vfr"]
        command += ["-c:v", "png", "-pix_fmt", "rgb24", str(path)]
        subprocess.run(command, check=True, capture_output=True, timeout=30)
        return path
    return make


@pytest.fixture
def make_excel(tmp_path):
    def make(rows, name="labels.xlsx") -> Path:
        from openpyxl import Workbook
        path = tmp_path / name
        wb = Workbook()
        ws = wb.active
        ws.append(["video", "violation", "timestamp", "group"])
        for row in rows:
            ws.append(row)
        wb.save(path)
        wb.close()
        return path
    return make
