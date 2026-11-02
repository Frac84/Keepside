"""Local label interactions with synthetic content and disabled model workers."""
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QStyleOptionViewItem

from snapvault import ui
from snapvault.classification import LEGACY_TOPIC_LABELS, TOPIC_LABELS
from snapvault.gallery import card_topics
from test_fluent_ui import app, flush, visible_ids, window


def select(window, key):
    window.refresh()
    window.gallery.setCurrentRow(visible_ids(window).index(key))


def test_topics_overlap_and_compose_with_type_search_and_favorites(window):
    first, _ = window.vault.add_text("Synthetic multi-topic learning reference")
    second, _ = window.vault.add_text("Synthetic receipt reference")
    unknown, _ = window.vault.add_text("Synthetic unfiled reference")
    window.apply_tags([first], ["tech", "learning"])
    window.apply_tags([second], ["receipts"])
    window.apply_tags([unknown], [])
    window.vault.toggle_favorite(first)
    window.set_item_type("text")
    window.set_topic("learning")
    assert visible_ids(window) == [first]
    window.set_topic("tech")
    assert visible_ids(window) == [first]
    window.set_scope("favorites")
    window.search.setText("learning reference")
    window.search_timer.stop()
    window.refresh()
    assert visible_ids(window) == [first]
    assert window.topic_buttons["tech"].isChecked()
    assert window.type_filter.currentData() == "text"
    assert window.topic_counts["tech"].text() == "1"
    assert window.topic_counts["learning"].text() == "1"
    window.set_item_type("image")
    assert not visible_ids(window)
    window.set_scope("all")
    window.search.clear()
    window.search_timer.stop()
    window.set_item_type("text")
    window.set_topic("unknown")
    assert visible_ids(window) == [unknown]
    assert window.detail_tags.text() == "待整理"
    assert "手动" in window.topic_status.text()


def test_label_editor_allows_manual_pending_but_limits_to_two_topics(app):
    dialog = ui.TopicDialog(["tech", "learning"])
    try:
        save = dialog.actions.button(QDialogButtonBox.StandardButton.Ok)
        assert dialog.tags() == ["tech", "learning"]
        assert save.isEnabled()
        dialog.checks["design"].setChecked(True)
        assert not save.isEnabled()
        assert "最多" in dialog.selection_hint.text()
        dialog.clear_tags()
        assert dialog.tags() == [] and save.isEnabled()
        assert list(dialog.checks) == list(TOPIC_LABELS)
    finally:
        dialog.deleteLater()
        flush(app)


def test_label_editor_preserves_existing_legacy_label_without_offering_unused_ones(app):
    dialog = ui.TopicDialog(["legacy_read"])
    try:
        assert dialog.tags() == ["legacy_read"]
        assert "legacy_read" in dialog.checks
        assert not set(LEGACY_TOPIC_LABELS).difference({"legacy_read"}).intersection(dialog.checks)
    finally:
        dialog.deleteLater()
        flush(app)


def test_batch_context_edit_keeps_selection_and_applies_to_all_selected(window, monkeypatch):
    keys = [window.vault.add_text(f"Synthetic batch note {index}")[0] for index in range(2)]
    window.set_item_type("text")
    window.gallery.clearSelection()
    for key in keys:
        window.gallery.item(visible_ids(window).index(key)).setSelected(True)
    observed = {}

    class ChoosingTags(ui.TopicDialog):
        def exec(self):
            observed["title"] = self.windowTitle()
            self.checks["design"].setChecked(True)
            self.checks["learning"].setChecked(True)
            return QDialog.DialogCode.Accepted

    class ChoosingMenu(ui.QMenu):
        def exec(self, *_):
            observed["actions"] = [action.text() for action in self.actions()]
            self.actions()[0].trigger()

    monkeypatch.setattr(ui, "TopicDialog", ChoosingTags)
    monkeypatch.setattr(ui, "QMenu", ChoosingMenu)
    item = window.gallery.item(visible_ids(window).index(keys[0]))
    window.gallery_menu(window.gallery.visualItemRect(item).center())
    assert "2 条" in observed["title"]
    assert "2 条" in observed["actions"][0]
    assert "恢复自动分类" in observed["actions"]
    assert set(window.selected_keys()) == set(keys)
    for key in keys:
        row = window.vault.get(key)
        assert row["tags"] == ["learning", "design"]
        assert row["tags_manual"]


def test_restore_automatic_clears_manual_lock_and_emits_only_selected_keys(window):
    first, _ = window.vault.add_text("Synthetic manual label reset")
    second, _ = window.vault.add_text("Synthetic other manual label")
    window.apply_tags([first], ["design"])
    window.apply_tags([second], ["receipts"])
    queued = []
    window.classif_requested.connect(queued.append)
    window.restore_auto_classification([first])
    row = window.vault.get(first)
    assert not row["tags_manual"]
    assert row["classification_status"] == "pending"
    assert row["tags"] == ["design"]  # Keep the previous label while waiting.
    assert queued == [[first]]
    assert window.vault.get(second)["tags_manual"]


def test_code_font_depends_on_content_instead_of_topic(window):
    prose, _ = window.vault.add_text("这是一段关于服务器架构的普通说明文字。")
    code, _ = window.vault.add_text("def example():\n    return 42")
    window.apply_tags([prose], ["tech"])
    window.apply_tags([code], ["learning"])
    select(window, prose)
    assert "Consolas" not in window.ocr_text.styleSheet()
    select(window, code)
    assert "Consolas" in window.ocr_text.styleSheet()


def test_old_manual_category_has_visible_filter_only_when_used(window):
    assert all(window.topic_buttons[key].isHidden() for key in LEGACY_TOPIC_LABELS)
    key, _ = window.vault.add_text("Synthetic historical shopping record")
    window.vault.set_category(key, "shopping")
    window.refresh()
    assert not window.topic_buttons["legacy_shopping"].isHidden()
    assert window.topic_buttons["legacy_read"].isHidden()
    window.set_topic("legacy_shopping")
    assert visible_ids(window) == [key]
    assert "购物（原分类）" in window.detail_tags.text()
    assert "手动" in window.topic_status.text()


def test_cards_show_two_topics_and_a_separate_classification_state(window, monkeypatch):
    key, _ = window.vault.add_text("Synthetic card badge rendering")
    window.apply_tags([key], ["tech", "learning"])
    window.restore_auto_classification([key])
    window.set_item_type("text")
    item = window.gallery.item(visible_ids(window).index(key))
    badges = []
    original = window.card_delegate._badge

    def record(painter, x, y, text, background, foreground):
        badges.append(text)
        return original(painter, x, y, text, background, foreground)

    monkeypatch.setattr(window.card_delegate, "_badge", record)
    canvas = QPixmap(280, 300)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 270, 290)
    try:
        window.card_delegate.paint(painter, option, window.gallery.indexFromItem(item))
    finally:
        painter.end()
    assert {"文字", "技术开发", "学习资料", "待分类"}.issubset(badges)
    assert card_topics({"tags": [], "classification_status": "done"}) == ["待整理"]
    assert card_topics({"tags": [], "classification_status": "pending"}) == ["待分类"]
    assert card_topics({"tags": [], "classification_status": "error"}) == ["分类失败"]
