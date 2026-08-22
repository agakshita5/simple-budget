import os
import sys
import subprocess
from pydantic import BaseModel, Field, ValidationError
from groq import Groq
import logging
from dotenv import load_dotenv
import json

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

# ====Pydantic Schema for Structured Output====
class FailurePoint(BaseModel):
    location: str = Field(description="File and line number/function where failure may occur")
    failure_reason: str = Field(description="How and why the code will break or throw an unhandled exception")

class ChaosReview(BaseModel):
    breaking_points: list[FailurePoint] = Field(
        description="Top 2 critical failure points in the diff. Empty list if safe.",
        max_length=2
    )

# ====Extract Git Diff from Runner Environment====
def get_git_diff() -> str:
    before, after = os.getenv("BEFORE_SHA"), os.getenv("AFTER_SHA")
    empty = "0000000000000000000000000000000000000000"

    if before and after and before != empty:
        rng = [f"{before}..{after}"]  # normal push
    elif after:  # zero-SHA case
        rng = [f"{after}^!", ]  # new branch: just the tip commit
    else:
        rng = ["HEAD~1", "HEAD"]  # local runs

    out = subprocess.run(["git", "diff", *rng], capture_output=True, text=True)
    return out.stdout.strip()[:30000]

# zero-SHA branch — that's what github.event.before contains on the first push to a new branch.

# ====LLM Review Execution====

def run_review(diff_text: str):
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("[Error] GROQ_API_KEY environment variable is missing.")
        logging.error("GROQ_API_KEY environment variable is missing")
        sys.exit(1)

    if not diff_text:
        print("[Info] Empty diff detected. Skipping LLM review.")
        logging.info("Empty diff detected. Skipping LLM review")
        sys.exit(0)

    client = Groq(api_key = api_key)

    system_prompt = (
        "You are a strict chaos-testing assistant. Do not suggest production scaling, "
        "refactoring, style guide fixes, or formatting changes. Analyze the provided git diff "
        "and identify ONLY the top 2 places where this code change will logically break, "
        "raise an unhandled exception, or cause a runtime failure."
    )

    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Review this git diff:\n\n{diff_text}"}
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "chaos_review",
                    "schema": ChaosReview.model_json_schema()
                }
            },
            temperature=0.2
        )

        # Parse output using structured schema
        review_data = ChaosReview.model_validate_json(response.choices[0].message.content)

        print("\n--- CHAOS REVIEW FINDINGS ---")
        if not review_data.breaking_points:
            print("No immediate runtime breaking points detected.")
        else:
            for idx, item in enumerate(review_data.breaking_points, start=1):
                print(f"\n[{idx}] Location: {item.location}")
                print(f"    Failure Reason: {item.failure_reason}")
        print("-----------------------------\n")

    except (ValidationError, json.JSONDecodeError, NameError, AttributeError) as e:
        logging.error(f"review agent is broken: {e}")
        sys.exit(1)
    except Exception as e:
        logging.error(f"LLM unavailable: {e}")
        sys.exit(0)


if __name__ == "__main__":
    diff = get_git_diff()
    run_review(diff)