"""Skill badges on Solana devnet: finishing a how-to mints a real 1-of-1 token to the kid's badge wallet.

    python -m brain.badges --setup          # make Teddy's + the kid's devnet keypairs, ask the faucet for SOL
    python -m brain.badges --mint "Tied My Shoes!"

Each badge is its own SPL mint: 0 decimals, 1 token minted to the kid's wallet, mint authority removed
(so it can never be copied), plus an on-chain memo "Teddy skill badge: Tied My Shoes! · Maya · date".
The iPad shows a QR to the transaction on Solana Explorer. Devnet only: no real money, ever.
Feature-flagged: no keypair or no SOL -> the badge is still saved in Snowflake/Tiger, just not on-chain.
"""
import asyncio
import base64
import datetime as dt
import io
import json
import os
import sys

from brain import flags

# 'confirmed', not the default 'finalized': a just-funded wallet otherwise looks empty for ~15 s

RPC = os.getenv("TEDDY_SOLANA_RPC", "https://api.devnet.solana.com")  # a local solana-test-validator works too
KID_KEYPAIR = flags.SOLANA_KEYPAIR.with_name("kid-badges.json")


def _client():
    from solana.rpc.async_api import AsyncClient
    from solana.rpc.commitment import Confirmed
    return AsyncClient(RPC, commitment=Confirmed)


def _kp(path):
    from solders.keypair import Keypair
    return Keypair.from_bytes(bytes(json.loads(path.read_text())))


def _save(kp, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(bytes(kp))))
    path.chmod(0o600)


def enabled():
    return flags.SOLANA_KEYPAIR.exists() and flags._on("solana")


def explorer(kind, value):
    return f"https://explorer.solana.com/{kind}/{value}?cluster=devnet"


def qr_data_uri(text):
    import qrcode
    img = qrcode.make(text, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def balance():
    from solana.rpc.async_api import AsyncClient

    async def go():
        async with _client() as c:
            return (await c.get_balance(_kp(flags.SOLANA_KEYPAIR).pubkey())).value / 1e9
    return asyncio.run(go())


def setup(sol=1.0):
    from solana.rpc.async_api import AsyncClient
    from solders.keypair import Keypair
    for path in (flags.SOLANA_KEYPAIR, KID_KEYPAIR):
        if not path.exists():
            _save(Keypair(), path)
    payer, kid = _kp(flags.SOLANA_KEYPAIR), _kp(KID_KEYPAIR)
    print(f"Teddy (pays for badges): {payer.pubkey()}")
    print(f"{flags.KID_NAME}'s badge wallet:  {kid.pubkey()}  {explorer('address', kid.pubkey())}")

    async def go():
        async with _client() as c:
            bal = (await c.get_balance(payer.pubkey())).value / 1e9
            print(f"Balance: {bal} SOL (devnet)")
            if bal >= 0.05:
                return
            try:
                sig = (await c.request_airdrop(payer.pubkey(), int(sol * 1e9))).value
                await c.confirm_transaction(sig)
                print(f"Airdropped {sol} SOL: {explorer('tx', sig)}")
            except Exception as e:
                print(f"Faucet said no ({str(e)[:160]}).\n  -> Get free devnet SOL at https://faucet.solana.com "
                      f"for {payer.pubkey()}")
    asyncio.run(go())
    return str(payer.pubkey()), str(kid.pubkey())


def mint(badge, skill, kid=flags.KID_NAME):
    """-> {"ok", "mint", "sig", "url", "qr", "wallet"} or {"ok": False, "reason"}."""
    if not enabled():
        return {"ok": False, "reason": "solana not set up (python -m brain.badges --setup)", "badge": badge}
    try:
        from solana.rpc.async_api import AsyncClient
        from solana.rpc.commitment import Confirmed
        from solders.keypair import Keypair
        from solders.message import MessageV0
        from solders.system_program import CreateAccountParams, create_account
        from solders.transaction import VersionedTransaction
        from spl.memo.constants import MEMO_PROGRAM_ID
        from spl.memo.instructions import create_memo
        from spl.memo.models import MemoParams
        from spl.token.constants import TOKEN_PROGRAM_ID
        from spl.token.instructions import (AuthorityType, create_associated_token_account,
                                            get_associated_token_address, initialize_mint, mint_to, set_authority)
        from spl.token.models import InitializeMintParams, MintToParams, SetAuthorityParams

        payer = _kp(flags.SOLANA_KEYPAIR)
        owner = _kp(KID_KEYPAIR).pubkey() if KID_KEYPAIR.exists() else payer.pubkey()
        mint_kp = Keypair()
        rent = 1461600  # rent-exempt minimum for an 82-byte mint account
        ata = get_associated_token_address(owner, mint_kp.pubkey())
        note = f"Teddy skill badge: {badge} · {kid} · {skill} · {dt.date.today().isoformat()}"
        ixs = [
            create_account(CreateAccountParams(from_pubkey=payer.pubkey(), to_pubkey=mint_kp.pubkey(), lamports=rent,
                                               space=82, owner=TOKEN_PROGRAM_ID)),
            initialize_mint(InitializeMintParams(decimals=0, program_id=TOKEN_PROGRAM_ID, mint=mint_kp.pubkey(),
                                                 mint_authority=payer.pubkey(), freeze_authority=None)),
            create_associated_token_account(payer.pubkey(), owner, mint_kp.pubkey()),
            mint_to(MintToParams(program_id=TOKEN_PROGRAM_ID, mint=mint_kp.pubkey(), dest=ata,
                                 mint_authority=payer.pubkey(), amount=1)),
            set_authority(SetAuthorityParams(program_id=TOKEN_PROGRAM_ID, account=mint_kp.pubkey(),
                                             authority=AuthorityType.MINT_TOKENS, current_authority=payer.pubkey(),
                                             new_authority=None)),
            create_memo(MemoParams(program_id=MEMO_PROGRAM_ID, signer=payer.pubkey(), message=note.encode())),
        ]
        async def go():
            async with _client() as c:
                blockhash = (await c.get_latest_blockhash()).value.blockhash
                tx = VersionedTransaction(MessageV0.try_compile(payer.pubkey(), ixs, [], blockhash), [payer, mint_kp])
                sim = (await c.simulate_transaction(tx)).value
                if sim.err:
                    raise RuntimeError(f"simulation failed: {sim.err} {(sim.logs or [])[-3:]}")
                sig = (await c.send_transaction(tx)).value
                await c.confirm_transaction(sig, commitment=Confirmed)
                return sig
        sig = asyncio.run(go())
        url = explorer("tx", sig)
        return {"ok": True, "badge": badge, "mint": str(mint_kp.pubkey()), "sig": str(sig), "url": url,
                "qr": qr_data_uri(url), "wallet": str(owner), "wallet_url": explorer("address", owner)}
    except Exception as e:
        print(f"[badges] mint failed: {e}")
        return {"ok": False, "reason": str(e)[:200], "badge": badge}


if __name__ == "__main__":
    if "--setup" in sys.argv:
        setup()
    elif "--mint" in sys.argv:
        name = sys.argv[sys.argv.index("--mint") + 1] if len(sys.argv) > sys.argv.index("--mint") + 1 else "Test Badge"
        r = mint(name, "testing")
        print({k: v for k, v in r.items() if k != "qr"})
