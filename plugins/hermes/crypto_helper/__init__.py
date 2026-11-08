import hashlib

def sha256_hash(text: str) -> str:
    """Compute SHA-256 hash of a text string."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def register(ctx):
    schema = {
        "name": "sha256_hash",
        "description": "Compute SHA-256 hash of a string",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to hash"}
            },
            "required": ["text"]
        }
    }
    
    async def handler(args, context=None):
        text = args.get("text", "")
        return sha256_hash(text)
        
    ctx.register_tool("sha256_hash", schema, handler)
