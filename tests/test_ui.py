from __future__ import annotations

import unittest

from nicegui.testing import user_simulation

from shiritori.page import register_pages


class UserInterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_word_and_chain_error_are_shown(self) -> None:
        async with user_simulation() as user:
            register_pages()
            await user.open("/")
            await user.should_see("いまのことば")
            await user.should_see("しりとり")
            await user.should_see("次のことば")

            user.find("次のことば").type("すいか").trigger("keydown.enter")
            await user.should_see("「り」から始まることばを入力してください。")
            await user.should_see("しりとり")

            user.find("次のことば").clear().type("りんご").trigger(
                "keydown.enter"
            )
            await user.should_see("「りんご」をつなぎました。次は「ご」です。")
            await user.should_see("りんご")

    async def test_game_over_can_be_reset(self) -> None:
        async with user_simulation() as user:
            register_pages()
            await user.open("/")

            user.find("次のことば").type("りぼん").trigger("keydown.enter")
            await user.should_see("「りぼん」は「ん」で終わるため、ゲーム終了です。")
            await user.should_see("ゲーム終了")

            user.find("もう一度").click()
            await user.should_see("最初のことばは「しりとり」。")
            await user.should_see("プレイ中")

            user.find("次のことば").type("りす").trigger("keydown.enter")
            await user.should_see("「りす」をつなぎました。次は「す」です。")

    async def test_duplicate_word_ends_the_game(self) -> None:
        async with user_simulation() as user:
            register_pages()
            await user.open("/")

            for word in ("りす", "すり", "りす"):
                user.find("次のことば").type(word).trigger("keydown.enter")

            await user.should_see("「りす」はすでに使われています。ゲーム終了です。")
            await user.should_see("ゲーム終了")


if __name__ == "__main__":
    unittest.main()
