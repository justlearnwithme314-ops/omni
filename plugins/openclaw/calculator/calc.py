import sys, json

def main():
    try:
        raw = sys.stdin.read()
        args = json.loads(raw) if raw.strip() else {}
        expr = args.get('expression', '2 + 2')
        # Safe eval
        allowed = {"__builtins__": None}
        result = eval(expr, allowed, {})
        print(json.dumps({"result": result, "expression": expr}))
    except Exception as e:
        print(json.dumps({"error": str(e)}))

if __name__ == '__main__':
    main()
