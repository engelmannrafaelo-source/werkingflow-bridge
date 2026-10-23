#!/usr/bin/env python3
"""Isolated synthetic capacity measurement on the GPU host, never a live deploy.

Runs the specified image/dependencies with the explicit source tree read-only.
Only numeric resource data, fixed status fields and image/source identity leave
the containers. No network, credentials, customer files or service mutations.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

RUNNER = r"""
import asyncio, json, sys
sys.path.insert(0, "/app")
from pathlib import Path
from src.privacy_service.document_process import run, write_json
root=Path("/work/call")
root.mkdir(parents=True)
objects=[b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
pages=[]
for n in range(int(sys.argv[1])):
    lines=["BT /F1 11 Tf 40 780 Td 16 TL"]
    for line in range(30):
        lines.append("(Synthetic page %d row %d. Fictional pumps and valves.) Tj T*" % (n,line))
    lines.append("ET")
    data=" ".join(lines).encode("ascii")
    content=len(objects)+1
    objects.append(b"<< /Length "+str(len(data)).encode()+b" >>\nstream\n"+data+b"\nendstream")
    page=len(objects)+1
    pages.append(page)
    objects.append(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        "/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content).encode())
objects[0]=b"<< /Type /Catalog /Pages 2 0 R >>"
objects[1]=("<< /Type /Pages /Count %d /Kids [%s] >>" %
    (len(pages)," ".join("%d 0 R" % p for p in pages))).encode()
pdf=bytearray(b"%PDF-1.4\n")
offsets=[0]
for n, obj in enumerate(objects,1):
    offsets.append(len(pdf));pdf.extend(("%d 0 obj\n" % n).encode()+obj+b"\nendobj\n")
xref=len(pdf)
pdf.extend(("xref\n0 %d\n0000000000 65535 f \n" % len(offsets)).encode())
for offset in offsets[1:]:pdf.extend(("%010d 00000 n \n" % offset).encode())
pdf.extend(("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" %
    (len(offsets),xref)).encode())
(root/"source").write_bytes(pdf)
write_json(root/"request.json", {"filename":"synthetic.pdf","mime_type":"application/pdf",
    "fields":{"describe_images":"false"},"managed":True,"repair_actualtext":False})
write_json(root/"accepted.json", {"deadline_at":None})
async def main():
    receipt=await run(root,asyncio.Event())
    result_path=root/"result.json"
    envelope=json.loads(result_path.read_text()) if result_path.exists() else {}
    result=envelope.get("result") or {}
    metadata=result.get("metadata") or {}
    print(json.dumps({"receipt":receipt,"status_code":envelope.get("status_code"),
        "success":result.get("success") is True,"metadata_keys":sorted(metadata),
        "docling_parsed":metadata.get("docling_parsed"),
        "page_count":result.get("page_count",metadata.get("page_count")),
        "conversion_time_seconds":result.get("conversion_time_seconds")}), flush=True)
asyncio.run(main())
"""


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()


def inspect(name):
    return json.loads(command("docker", "inspect", "--format", "{{json .State}}", name))


def tree(root):
    """Only our container's process tree, no unrelated process arguments."""
    seen = set()
    pending = [root]
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            for thread in Path(f"/proc/{pid}/task").iterdir():
                pending.extend(int(p) for p in (thread / "children").read_text().split())
        except (FileNotFoundError, ProcessLookupError):
            pass
    return seen


def memory(pids):
    rss = {}
    for pid in pids:
        try:
            lines = Path(f"/proc/{pid}/status").read_text().splitlines()
            values = {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in lines if ":" in line}
            start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            rss[f"{pid}:{start}"] = int(values.get("VmRSS", "0 kB").split()[0]) * 1024
        except (FileNotFoundError, ProcessLookupError):
            pass
    return rss


def gpu_memory():
    rows = command("nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
                   "--format=csv,noheader,nounits")
    values = {}
    for row in rows.splitlines():
        pid, mib = row.split(",")
        values[int(pid.strip())] = values.get(int(pid.strip()), 0) + int(mib.strip()) * 1024**2
    return values


def gpu_free():
    rows = command("nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits")
    return min(int(row.strip()) for row in rows.splitlines()) * 1024**2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--pages", type=int, default=25)
    parser.add_argument("--parallel", type=int, choices=(1, 2), default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.pages <= 1000:
        parser.error("pages must be 1..1000 for this synthetic measurement")
    image = command("docker", "image", "inspect", "--format", "{{.Id}}", args.image)
    if gpu_free() < 8192 * 1024**2:
        raise RuntimeError("Measurement not started: less than 8 GiB GPU headroom")
    names = []
    records = []
    started = time.monotonic()
    overall_peak = 0
    overall_gpu_peak = 0
    reason = None
    report = {"image_id": image, "source_sha": args.source_sha,
              "pages_each": args.pages, "parallel": args.parallel,
              "sample_interval_seconds": 0.25, "memory_limit_each_bytes": 2 * 1024**3,
              "network": "none", "records": records}
    with tempfile.TemporaryDirectory(prefix="document-capacity-") as temporary:
        runner = Path(temporary) / "runner.py"
        runner.write_text(RUNNER)
        try:
            for _ in range(args.parallel):
                name = "document-capacity-" + uuid.uuid4().hex[:12]
                names.append(name)
                command("docker", "run", "-d", "--name", name,
                    "--network", "none", "--gpus", "all", "--memory", "2g",
                    "--memory-swap", "2g", "--cpus", "2", "--pids-limit", "256",
                    "--mount", f"type=bind,src={args.source_root.resolve() / 'src'},dst=/app/src,readonly",
                    "--mount", f"type=bind,src={runner},dst=/probe.py,readonly",
                    "--tmpfs", "/work:rw,size=256m",
                    "--entrypoint", "/app/.venv/bin/python", image, "/probe.py", str(args.pages))
                records.append({"name": name, "rss_tree_peak_bytes": 0, "gpu_peak_bytes": 0,
                                "rss_process_peaks_bytes": {}, "gpu_process_peaks_bytes": {}})
            while True:
                states = [inspect(name) for name in names]
                if not any(state["Running"] for state in states):
                    break
                gpu = gpu_memory()
                total = 0
                total_gpu = 0
                for state, record in zip(states, records):
                    if not state["Running"]:
                        continue
                    pids = tree(state["Pid"])
                    rss = memory(pids)
                    used_gpu = {pid: used for pid, used in gpu.items() if pid in pids}
                    total += sum(rss.values())
                    total_gpu += sum(used_gpu.values())
                    record["rss_tree_peak_bytes"] = max(record["rss_tree_peak_bytes"], sum(rss.values()))
                    record["gpu_peak_bytes"] = max(record["gpu_peak_bytes"], sum(used_gpu.values()))
                    for pid, used in rss.items():
                        held = record["rss_process_peaks_bytes"]
                        held[pid] = max(held.get(pid, 0), used)
                    for pid, used in used_gpu.items():
                        held = record["gpu_process_peaks_bytes"]
                        held[str(pid)] = max(held.get(str(pid), 0), used)
                overall_peak = max(overall_peak, total)
                overall_gpu_peak = max(overall_gpu_peak, total_gpu)
                if gpu_free() < 2048 * 1024**2:
                    reason = "measurement_stopped_gpu_headroom"
                    break
                time.sleep(0.25)
        except Exception as exc:
            reason = "measurement_error_" + type(exc).__name__
        finally:
            for name, record in zip(names, records):
                state = inspect(name)
                if state["Running"]:
                    command("docker", "kill", name)
                    state = inspect(name)
                record.update({"exit_code": state["ExitCode"], "oom_killed": state["OOMKilled"]})
                # Our runner prints one JSON line. Never emit raw converter logs.
                raw = command("docker", "logs", name)
                try:
                    record["result"] = json.loads(raw.splitlines()[-1])
                except (ValueError, IndexError):
                    record["result"] = {"diagnostic": "runner_result_missing"}
            for name in names:
                subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
            if reason is None and any(
                record["oom_killed"] or record["exit_code"] != 0
                or record["result"].get("success") is not True
                or record["result"].get("docling_parsed") is not True
                for record in records
            ):
                reason = "conversion_or_docling_unproven"
            report.update({"duration_seconds": round(time.monotonic()-started, 2),
                "rss_parallel_peak_bytes": overall_peak, "gpu_parallel_peak_bytes": overall_gpu_peak,
                "measurement_stop_reason": reason})
            args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "measurement_stop_reason": reason,
                      "conversions": len(records)}))
    if reason:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
