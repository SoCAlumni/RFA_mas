"""Opt-in public-synthetic model comparison; never edits the selected env profile.

The existing E2E transport guard still rejects private markers and other hosts.
Set RFA_ENV_FILE and RFA_E2E_REPORT_DIR explicitly. This consumes NVIDIA quota.
"""

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-output-tokens", type=int, required=True)
    parser.add_argument("--model", choices=["nvidia/nemotron-3-ultra-550b-a55b"])
    args = parser.parse_args()
    if not 1024 <= args.max_output_tokens <= 16384:
        parser.error("max-output-tokens must be between 1024 and 16384")
    import pytest

    harness = Path(__file__).resolve().parents[1] / "tests/e2e/test_real_model.py"

    class Comparison:
        def pytest_collection_modifyitems(self, items):
            modules = {item.module for item in items if item.module.__file__ == str(harness)}
            if len(modules) != 1:
                raise RuntimeError("Expected exactly one reviewed E2E harness module")
            for module in modules:
                module.REAL_LIMITS["nvidia_max_output_tokens"] = args.max_output_tokens
                module.SCOPE += (
                    f"; explicit experiment max_output_tokens={args.max_output_tokens}; "
                    "env file unchanged"
                )
                if args.model:
                    original = module.configured

                    def configured(original=original):
                        overrides, reason = original()
                        if overrides is not None:
                            overrides["nvidia_model"] = args.model
                        return overrides, reason

                    module.configured = configured
                    module.SCOPE += "; model override=" + args.model

    return pytest.main(["-q", str(harness)], plugins=[Comparison()])


if __name__ == "__main__":
    raise SystemExit(main())
