#!/usr/bin/env python3
"""DeepSeek / OpenCode Zen Go CLI & Parallel Worker Tool for Antigravity & Gemini Orchestrators.

Powered directly by OpenCode Zen Go gateway:
- Model: deepseek-v4.1-flash
- Concurrent multi-worker thread pool execution
- Direct code extraction and file output
"""

import argparse
import concurrent.futures
import json
import os
import sys
import time
import uuid
from pathlib import Path

try:
    import httpx
except ImportError:
    import subprocess
    cmd = ["uv", "run", "python", __file__] + sys.argv[1:]
    res = subprocess.run(cmd)
    sys.exit(res.returncode)


def load_keys():
    evren_key = os.environ.get("EVREN_API_KEY", "")
    if not evren_key:
        evren_path = Path("~/.config/opencode/evren.key").expanduser()
        if evren_path.exists():
            evren_key = evren_path.read_text().strip()

    opencode_key = os.environ.get("OPENCODE_API_KEY", os.environ.get("LLM_API_KEY", ""))
    if not opencode_key:
        tlab_env = Path("/Users/eniskorkut/Desktop/trading-system/tlab/.env")
        if tlab_env.exists():
            for line in tlab_env.read_text().splitlines():
                if line.startswith("OPENCODE_API_KEY=") or line.startswith("LLM_API_KEY="):
                    opencode_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if opencode_key:
                        break
    if not opencode_key:
        # Gömülü anahtar yok: OPENCODE_API_KEY env'i veya tlab/.env gerekli.
        sys.stderr.write("UYARI: OPENCODE_API_KEY bulunamadi (env veya tlab/.env).\n")
    return evren_key, opencode_key


def call_llm(prompt: str, system_prompt: str = "", model: str = "deepseek-v4.1-flash",
             thinking: bool = True, reasoning_effort: str = "max",
             temperature: float = 0.2, max_tokens: int = 16000,
             worker_id: str = "worker-1") -> str:
    evren_key, opencode_key = load_keys()

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    # Try 1: Evren Primary Gateway (Fast 15s check, failover immediately on 429 or timeout)
    if evren_key:
        evren_body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        evren_headers = {
            "Authorization": f"Bearer {evren_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=httpx.Timeout(15.0, connect=8.0)) as client:
                resp = client.post("https://evren-llmapi.ssyz.org.tr/v1/chat/completions",
                                   json=evren_body, headers=evren_headers)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"].get("content", "")
                if content:
                    return content
        except Exception as e:
            sys.stderr.write(f"[{worker_id}] Evren unavailable/busy ({e}), falling back to OpenCode...\n")

    # Try 2: OpenCode Zen Go Fallback Gateway
    opencode_base = "https://opencode.ai/zen/go/v1"
    opencode_body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }
    opencode_headers = {
        "Authorization": f"Bearer {opencode_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-opencode-session": f"ses_{uuid.uuid4()}",
    }

    for attempt in range(2):
        try:
            with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
                resp = client.post(f"{opencode_base}/chat/completions", json=opencode_body, headers=opencode_headers)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"].get("content", "")
        except Exception as e:
            sys.stderr.write(f"[{worker_id}] OpenCode attempt {attempt+1} failed: {e}\n")
            if attempt == 0:
                time.sleep(1.5)

    raise RuntimeError(f"[{worker_id}] Both Evren and OpenCode gateways failed after retries.")


def extract_code_block(text: str) -> str:
    import re
    dart_blocks = re.findall(r"```dart\s*\n(.*?)```", text, re.DOTALL)
    if dart_blocks:
        largest_dart = max(dart_blocks, key=len).strip()
        if largest_dart:
            return largest_dart

    all_blocks = re.findall(r"```(?:[a-zA-Z0-9_\-]+)?\s*\n(.*?)```", text, re.DOTALL)
    if all_blocks:
        largest = max(all_blocks, key=len).strip()
        if largest:
            return largest

    if "```" in text:
        parts = text.split("```")
        blocks = [p.strip() for p in parts[1::2] if p.strip()]
        if blocks:
            largest = max(blocks, key=len)
            first_newline = largest.find("\n")
            if first_newline != -1 and first_newline < 30:
                stripped = largest[first_newline + 1:].strip()
                if stripped:
                    return stripped
            return largest
    return text.strip()


def run_parallel_jobs(batch_file: str, max_workers: int = 4):
    """Run multiple prompt jobs concurrently in parallel using DeepSeek workers."""
    with open(batch_file, "r") as f:
        jobs = json.load(f)

    print(f"[Orchestrator] Dispatching {len(jobs)} parallel DeepSeek worker tasks via OpenCode (max_workers={max_workers})...")
    results = {}

    def _worker(job):
        jid = job.get("id", str(uuid.uuid4())[:8])
        prompt = job["prompt"]
        system = job.get("system", "You are an expert quantitative developer and elite coder.")
        thinking = job.get("thinking", True)
        reasoning_effort = job.get("reasoning_effort", "max")
        out_path = job.get("output")
        extract_code = job.get("extract_code", False)

        print(f"[{jid}] Started DeepSeek worker (reasoning_effort={reasoning_effort})...")
        start_t = time.time()
        res = call_llm(prompt, system_prompt=system, thinking=thinking, reasoning_effort=reasoning_effort, worker_id=jid)
        elapsed = time.time() - start_t
        print(f"[{jid}] Finished in {elapsed:.1f}s")

        if extract_code:
            res = extract_code_block(res)

        if out_path:
            p = Path(out_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(res)
            print(f"[{jid}] Saved output to {out_path}")

        return jid, res

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_worker, job) for job in jobs]
        for future in concurrent.futures.as_completed(futures):
            jid, res = future.result()
            results[jid] = res

    print(f"[Orchestrator] All {len(jobs)} parallel DeepSeek tasks completed successfully.")
    return results


def main():
    parser = argparse.ArgumentParser(description="DeepSeek V4.1 Flash via OpenCode CLI")
    parser.add_argument("--prompt", "-p", help="User prompt string")
    parser.add_argument("--system", "-s", default="You are an expert quantitative developer and elite coder.", help="System prompt")
    parser.add_argument("--file", "-f", help="Path to input context file to append to prompt")
    parser.add_argument("--output", "-o", help="File path to save the generated output")
    parser.add_argument("--no-thinking", action="store_true", help="Disable reasoning/thinking")
    parser.add_argument("--reasoning-effort", default="max", choices=["low", "medium", "high", "max"], help="Reasoning effort level (default: max)")
    parser.add_argument("--max-tokens", type=int, default=16000, help="Max tokens budget for reasoning + content (default: 16000)")
    parser.add_argument("--extract-code", action="store_true", help="Extract code block if wrapped in markdown")
    parser.add_argument("--batch", "-b", help="Path to JSON file containing array of jobs to execute in parallel")
    parser.add_argument("--workers", "-w", type=int, default=4, help="Max concurrent parallel workers (default 4)")

    args = parser.parse_args()

    if args.batch:
        run_parallel_jobs(args.batch, max_workers=args.workers)
        return

    prompt_text = args.prompt or ""
    if not sys.stdin.isatty():
        stdin_content = sys.stdin.read().strip()
        if stdin_content:
            prompt_text = f"{prompt_text}\n\n{stdin_content}".strip()

    if args.file:
        file_path = Path(args.file)
        if file_path.exists():
            prompt_text = f"{prompt_text}\n\n--- Content of {file_path.name} ---\n{file_path.read_text()}".strip()

    if not prompt_text:
        parser.error("No prompt provided via --prompt, --file, --batch, or stdin.")

    result = call_llm(
        prompt=prompt_text,
        system_prompt=args.system,
        thinking=not args.no_thinking,
        reasoning_effort=args.reasoning_effort,
        max_tokens=args.max_tokens,
    )

    if args.extract_code:
        result = extract_code_block(result)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result)
        print(f"[DeepSeek Worker] Wrote output to {out_path}")
    else:
        print(result)


if __name__ == "__main__":
    main()
