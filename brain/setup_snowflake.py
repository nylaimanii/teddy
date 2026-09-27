"""One-shot Snowflake setup: tables, stages, PDFs -> AI_PARSE_DOCUMENT -> chunks -> Cortex Search (DOC_SEARCH),
semantic model for Cortex Analyst, and a check of which Cortex models this account can use.

    python -m brain.setup_snowflake            # everything
    python -m brain.setup_snowflake --docs     # just re-ingest data/docs/*.pdf
    python -m brain.setup_snowflake --models   # just test Cortex models
    python -m brain.setup_snowflake --howto    # just (re)load the kid how-to guides
"""
import sys
import time

from brain import snowflake as sf

SQL = sf.ROOT / "brain" / "sql"
DOCS = sf.ROOT / "data" / "docs"


def run_file(b, path):
    text = "\n".join(l for l in path.read_text().splitlines() if not l.strip().startswith("--"))
    for stmt in text.replace("{WH}", sf.WAREHOUSE).split(";"):
        body = stmt.strip()
        if not body:
            continue
        print(f"  > {body.splitlines()[0][:90]}")
        try:
            b.query(body, timeout=900)
        except Exception as e:
            if "CORTEX_ENABLED_CROSS_REGION" in body:
                print(f"    (skipped, needs ACCOUNTADMIN: {e})")
            else:
                raise


def setup(b):
    print("1) Tables, view, stages")
    run_file(b, SQL / "setup.sql")
    print("2) Semantic model for Cortex Analyst")
    b.query(f"PUT 'file://{SQL / 'teddy_semantic.yaml'}' @TEDDY.CORE.MODELS AUTO_COMPRESS=FALSE OVERWRITE=TRUE")


def docs(b):
    docs = sorted(f for ext in ("pdf", "txt", "md") for f in DOCS.glob(f"*.{ext}"))
    print(f"3) Uploading {len(docs)} docs (PDF/txt/md) from data/docs")
    if not docs:
        print("   No docs yet. Name them like cpr__aha_guide.pdf, first_aid__red_cross_burns.md, homework__fractions.pdf")
        return
    b.query("REMOVE @TEDDY.CORE.DOCS")  # stage mirrors the folder, so deleted docs leave the index too
    for p in docs:
        print(f"   PUT {p.name}")
        b.query(f"PUT 'file://{p}' @TEDDY.CORE.DOCS AUTO_COMPRESS=FALSE OVERWRITE=TRUE")
    print("4) AI_PARSE_DOCUMENT (PDF) + staged text (txt/md) -> 1500/200 chunks -> DOC_SEARCH (a minute or two)")
    run_file(b, SQL / "ingest_docs.sql")
    for r in b.query("SELECT DOMAIN, TITLE, COUNT(*) AS N, ANY_VALUE(SOURCE_URL) AS URL FROM DOC_CHUNKS "
                     "GROUP BY 1, 2 ORDER BY 1, 2"):
        print(f"   {r['domain']:10s} {r['n']:3d} chunks  {r['title']}" + (f"  <{r['url']}>" if r['url'] else ""))


def models(b):
    print("5) SHOW CORTEX BASE MODELS, then test the ones Teddy uses")
    names = {r["name"].lower() for r in b.query("SHOW CORTEX BASE MODELS IN ACCOUNT")}
    for m in ["llama3.3-70b", "mistral-large3", "mistral-large2", "llama3.1-8b"]:
        print(f"   {'listed' if m in names else 'MISSING'}  {m}")
    ok = []
    for m in ["llama3.3-70b", "mistral-large3", "mistral-large2", "llama3.1-8b"]:
        t = time.time()
        try:
            b.query("SELECT AI_COMPLETE(%s, 'Say hi in 3 words.') AS R", (m,), timeout=25)
            ok.append(m)
            print(f"   OK   {m:18s} {time.time() - t:.1f}s")
        except Exception as e:
            print(f"   --   {m:18s} {str(e).splitlines()[0][:80]}")
    if ok:
        print(f"   -> Teddy will use {ok[0]} (set TEDDY_CORTEX_MODEL in .env to override)")


def smoke(b):
    print("6) Smoke test ask()")
    for q, d in [("How fast do I push during CPR?", "cpr"), ("What do I do for a burn?", "first_aid"),
                 ("Someone is choking and can't talk, what do I do?", "first_aid"),
                 ("How can grandma avoid falling at home?", "first_aid"), ("Should I put ice on a burn?", None)]:
        r = sf.ask_detailed(q, d)
        print(f"   Q: {q}\n   A: {r['answer']}  [{r['model']}, {r['ms']}ms, {r.get('url') or ''}]")


if __name__ == "__main__":
    b = sf.configure()
    if b.name != "snowflake":
        sys.exit("Fill SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER / SNOWFLAKE_PASSWORD in .env first.")
    args = set(sys.argv[1:])
    if not args or "--setup" in args:
        setup(b)
    if not args or "--docs" in args:
        docs(b)
    if not args or "--howto" in args:
        from brain import howto
        print(f"5b) How-to guides -> HOWTO_GUIDES/HOWTO_STEPS + HOWTO_SEARCH: {howto.load_to_snowflake()} guides")
    if not args or "--models" in args:
        models(b)
    if not args or "--smoke" in args:
        smoke(b)
    time.sleep(1)  # let the async event writer flush
