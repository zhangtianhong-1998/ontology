"""A real stdio MCP server backed by local, explicitly synthetic documents."""
import argparse
import re

from mcp.server.fastmcp import FastMCP

from .storage import read_yaml


def tokens(text):
    text = text.casefold()
    return set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text))


def create_server(path):
    data = read_yaml(path)
    docs = {d["id"]: d for d in data.get("documents", [])}
    app = FastMCP("ontology-synthetic-knowledge")

    @app.tool()
    def search(query: str, limit: int = 5) -> dict:
        """Search synthetic enterprise documents; returns identifiers and snippets."""
        if data.get("simulate_error"):
            raise RuntimeError("Synthetic knowledge source failure")
        query_tokens = tokens(query)
        ranked = sorted(((len(query_tokens & tokens(d["title"] + " " + d["text"])), d) for d in docs.values()), key=lambda x: (-x[0], x[1]["id"]))
        return {"hits": [{"id": d["id"], "title": d["title"], "snippet": d["text"][:200]} for score, d in ranked if score > 0][:max(0, min(limit, 20))]}

    @app.tool()
    def fetch(document_id: str) -> dict:
        """Read a synthetic document with its scope, version and original text."""
        if document_id not in docs:
            raise ValueError("Unknown synthetic document")
        return docs[document_id]

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--documents", required=True)
    args = parser.parse_args()
    create_server(args.documents).run(transport="stdio")


if __name__ == "__main__":
    main()
