from __future__ import annotations

import unittest

from nicegui.slot import Slot
from nicegui.testing import user_simulation

from shiritori.page import register_pages


class UserInterfaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # NiceGUI 3.14 can retain a slot stack under a reused asyncio task ID
        # between IsolatedAsyncioTestCase methods (notably on Python 3.13).
        Slot.stacks.clear()

    async def test_free_first_word_and_reading_chain_are_shown(self) -> None:
        async with user_simulation() as user:
            register_pages()
            await user.open("/")
            await user.should_see("いまのことば")
            await user.should_see("最初のことばは自由です")
            await user.should_see("次のことば")

            user.find("次のことば").type("林檎").trigger("keydown.enter")
            await user.should_see("「林檎」を受け付けました。")
            await user.should_see("よみ: りんご")

            user.find("次のことば").type("すいか").trigger("keydown.enter")
            await user.should_see("「ご」から始まる単語を入力してください。")
            await user.should_see("林檎")

    async def test_dictionary_errors_are_visible_without_advancing(self) -> None:
        async with user_simulation() as user:
            register_pages()
            await user.open("/")

            user.find("次のことば").type("あ").trigger("keydown.enter")
            await user.should_see("ひらがな1文字だけの単語は使用できません。")
            await user.should_see("最初のことばは自由です")

    async def test_game_over_can_be_reset(self) -> None:
        async with user_simulation() as user:
            register_pages()
            await user.open("/")

            user.find("次のことば").type("リボン").trigger("keydown.enter")
            await user.should_see("「リボン」は「ん」で終わります。 ゲーム終了です。")
            await user.should_see("ゲーム終了")

            user.find("もう一度").click()
            await user.should_see("先攻は、辞書にある好きなことばから始められます。")
            await user.should_see("プレイ中")

            user.find("次のことば").type("林檎").trigger("keydown.enter")
            await user.should_see("「林檎」を受け付けました。")

    async def test_duplicate_reading_ends_the_game(self) -> None:
        async with user_simulation() as user:
            register_pages()
            await user.open("/")

            for word in ("りす", "すり", "りす"):
                user.find("次のことば").type(word).trigger("keydown.enter")

            await user.should_see("「りす」と同じ読みは使用済みです。 ゲーム終了です。")
            await user.should_see("ゲーム終了")


if __name__ == "__main__":
    unittest.main()