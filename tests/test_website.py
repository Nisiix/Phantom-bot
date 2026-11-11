from desk.website import wallet_secret_asks


def test_seed_phrase_safety_tips_are_not_scam():
    # Real "how to buy" texts that capped NIHAO, KoriMu, 咪好 and DAINU at 0.30 (2026-11-11).
    tips = [
        "Grab Phantom. Keep your recovery phrase secret, even from a very convincing cat.",
        "Install Phantom. Write the seed phrase on paper and never share it with anyone.",
        "Create a Solana wallet. Keep the seed phrase offline.",
        "Verify the contract before you buy, and never share a seed phrase.",
    ]
    for t in tips:
        assert wallet_secret_asks(t.lower()) == [], t


def test_asking_for_the_seed_phrase_is_scam():
    asks = [
        "Enter your seed phrase to claim the airdrop",
        "To sync your wallet, enter your secret recovery phrase below",
        "Validate your wallet: paste your 12 words here",
        "Import your private key to unlock rewards",
        "Enter your seed phrase. Never share it with anyone else.",
    ]
    for t in asks:
        assert wallet_secret_asks(t.lower()), t
