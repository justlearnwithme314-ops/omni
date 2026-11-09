// Pi Extension: sample-ui
module.exports = function(pi) {
    pi.registerTool({
        name: "format_banner",
        description: "Formats a text message as an ASCII banner",
        parameters: {
            type: "object",
            properties: {
                title: { type: "string", description: "Title for the banner" }
            },
            required: ["title"]
        },
        async execute(_toolCallId, args) {
            const t = args.title || "";
            const border = "=".repeat(t.length + 8);
            return border + "\n=== " + t + " ===\n" + border;
        }
    });
};
