"""Skill badges on Solana devnet (filled in with the minting code in the Solana step)."""
from brain import flags


def mint(badge, skill, kid=flags.KID_NAME):
    return {"ok": False, "reason": "solana not set up", "badge": badge, "skill": skill}
