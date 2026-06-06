"""
://agent_arena — AppWorld starter agent (ReAct code agent).

This is a WORKING template you can hack on. The loop and every AppWorld API
call below were verified against appworld==0.1.3. Your job is to make the agent
smarter: better prompting, planning, error recovery, retrieval, etc.

How AppWorld works (the rules your agent plays by):
  - Each task gives you a natural-language instruction from your "supervisor".
  - You act by writing PYTHON code. The env runs it and returns whatever you
    print(). A preloaded object `apis` is your only interface to the 9 apps.
  - Discover APIs at runtime:
        apis.api_docs.show_app_descriptions()
        apis.api_docs.show_api_descriptions(app_name='spotify')
        apis.api_docs.show_api_doc(app_name='spotify', api_name='login')
  - Get credentials to log into apps:
        apis.supervisor.show_account_passwords()
    (most app APIs need an access_token returned by that app's `login`).
  - Finish with:
        apis.supervisor.complete_task(answer=<answer or None>)
    Pass `answer` only when the task asks a question; otherwise leave it None.

Run:
  export OPENROUTER_API_KEY=sk-or-...         # or put it in .env
  export APPWORLD_EXPERIMENT=team_<yourname>   # your unique team id
  export APPWORLD_DATASET=dev                  # dev while building; switch to the
                                               # official split at submission time
  python agent.py
"""

import os
import re
import ast

try:  # optional: load OPENROUTER_API_KEY etc. from a local .env
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from appworld import AppWorld, load_task_ids
import litellm

# ---- config ---------------------------------------------------------------
# MODEL is litellm's "provider/model" string. This repo defaults to OpenRouter.
DEFAULT_OPENROUTER_MODEL = "openrouter/meta-llama/llama-3.3-70b-instruct"

MODEL_ALIASES = {
    "openrouter/llama-3.3-70b-instruct": DEFAULT_OPENROUTER_MODEL,
}

if "MODEL" in os.environ:
    MODEL = os.environ["MODEL"]
else:
    MODEL = os.environ.get("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)
MODEL = MODEL_ALIASES.get(MODEL, MODEL)
DATASET = os.environ.get("APPWORLD_DATASET", "dev")          # dev | test_normal | test_challenge
EXPERIMENT = os.environ.get("APPWORLD_EXPERIMENT", "team_demo")
MAX_INTERACTIONS = int(os.environ.get("MAX_INTERACTIONS", "30"))
MAX_TASKS = int(os.environ.get("MAX_TASKS", "0"))            # 0 = all tasks in split

SYSTEM_PROMPT = """You are an autonomous coding agent operating inside AppWorld.
You complete the supervisor's task by writing Python code that the environment executes.

RULES:
- Reply with EXACTLY ONE Python code block per turn, nothing else:
  ```python
  # your code
  ```
- A preloaded object `apis` is the ONLY way to interact with the apps. Whatever
  you print() is returned to you as the next observation.
- You do NOT know the APIs in advance. Discover them at runtime:
    print(apis.api_docs.show_app_descriptions())
    print(apis.api_docs.show_api_descriptions(app_name='<app>'))
    print(apis.api_docs.show_api_doc(app_name='<app>', api_name='<api>'))
- Before calling any app API for the first time, inspect its schema with
  show_api_doc and use the exact documented parameter names and response fields.
- To access any supervisor app account, first get that app's credentials:
    print(apis.supervisor.show_account_passwords())
    # Use the returned supervisor account id/password for the matching app's
    # login API before calling protected app APIs.
    # Save the returned access_token and pass that app-specific token whenever
    # the API doc requires it.
    # Always use the exact parameter names from show_api_doc; do not rename
    # them. For Gmail login, the email goes in username, not email:
    #     gmail_login = apis.gmail.login(username=<gmail_email>, password=<password>)
    #     gmail_access_token = gmail_login["access_token"]
    #     apis.gmail.show_drafts(access_token=<gmail_access_token>)
- Work in small steps: inspect results before the next action. Never invent API
  names or fields — look them up first.
- When and ONLY when the task is fully done, call:
    apis.supervisor.complete_task(answer=<answer>)   # answer=None if not a question
"""


def call_llm(messages: list[dict]) -> str:
    resp = litellm.completion(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, *messages],
        max_tokens=int(os.environ.get("MAX_TOKENS", "4096")),
        num_retries=8,   # ride out free-tier rate limits (429) with backoff
    )
    return resp.choices[0].message.content or ""


def extract_code(text: str) -> str:
    text = text.strip()
    m = re.search(r"```[ \t]*(?:python|py)?[ \t]*(?:\r?\n)?(.*?)```", text, re.S | re.I)
    if m:
        return m.group(1).strip()

    # Some models return an opening fence but forget the closing one.
    m = re.search(r"```[ \t]*(?:python|py)?[ \t]*(?:\r?\n)?(.*)", text, re.S | re.I)
    if m:
        return m.group(1).strip()

    return text.removeprefix("```python").removeprefix("```py").removeprefix("```").strip()


def solve(world: AppWorld) -> None:
    messages = [{
        "role": "user",
        "content": (
            f"Supervisor: {world.task.supervisor}\n\n"
            f"Task: {world.task.instruction}\n\n"
            "Begin. Remember: one python code block per turn."
        ),
    }]
    for step in range(MAX_INTERACTIONS):
        reply = call_llm(messages)
        code = extract_code(reply)
        try:
            ast.parse(code)
        except SyntaxError as e:
            output = (
                "Code extraction produced invalid Python before execution. "
                f"{e.__class__.__name__}: {e.msg} on line {e.lineno}. "
                "Reply again with exactly one valid Python code block and no prose."
            )
            print(f"  step {step+1}: skipped invalid code -> {output!r}")
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": f"Execution output:\n{output}"})
            continue
        output = world.execute(code)
        print(f"  step {step+1}: ran {len(code)} chars -> {str(output)[:120]!r}")
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": f"Execution output:\n{output}"})
        if world.task_completed():
            print("  ✓ task_completed")
            return
    print("  ✗ hit MAX_INTERACTIONS without completion")


def main() -> None:
    task_ids = load_task_ids(DATASET)
    if MAX_TASKS:
        task_ids = task_ids[:MAX_TASKS]
    print(f"Running '{EXPERIMENT}' on {len(task_ids)} '{DATASET}' tasks with {MODEL}")
    for i, task_id in enumerate(task_ids, 1):
        print(f"[{i}/{len(task_ids)}] {task_id}")
        with AppWorld(task_id=task_id, experiment_name=EXPERIMENT) as world:
            try:
                solve(world)
            except Exception as e:  # never let one task kill the whole run
                print(f"  ! error: {e}")
    print(f"\nDone. Outputs in ./experiments/outputs/{EXPERIMENT}/")
    print("Hand that folder to the organizers (or zip and submit per instructions).")


if __name__ == "__main__":
    main()
