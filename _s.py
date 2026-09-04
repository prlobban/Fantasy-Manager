import asyncio
from core.mcp_server import mcp
async def main():
    tools = await mcp.list_tools()
    reads = [t.name for t in tools if t.name.startswith("get_")]
    writes = [t.name for t in tools if not t.name.startswith("get_")]
    print("READ tools :", reads)
    print("WRITE tools:", writes)
    forbidden = {"counter_trade","set_league_settings","post_message","propose_trade","accept_trade","draft_player"}
    leaked = forbidden & {t.name for t in tools}
    print("\nforbidden tools exposed:", leaked or "NONE (correct)")
    print("total tools:", len(tools))
asyncio.run(main())
