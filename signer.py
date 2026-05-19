from eth_account import Account


class WalletSigner:
    def __init__(self, private_key: str) -> None:
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        self.private_key = private_key
