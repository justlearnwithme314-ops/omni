"""Run the whole suite quietly; exit non-zero on any failure (use as a publish gate)."""
import io, sys, unittest
res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(unittest.TestLoader().discover("tests", pattern="test_*.py"))
print(f"RESULT ran={res.testsRun} fail={len(res.failures)} err={len(res.errors)}", file=sys.stderr)
for t, tb in res.failures + res.errors:
    print("-", t.id().split(".")[-1], "|", tb.strip().splitlines()[-1][:200], file=sys.stderr)
sys.exit(0 if res.wasSuccessful() else 1)
