"""Tests for the per-thread directory-browser/picker state helpers.

RC13 / f31 / f33: this state used to live directly in ``context.user_data``
under flat keys (``STATE_KEY``, ``BROWSE_PATH_KEY``, ``_pending_thread_id``,
``_pending_thread_text``, ...) — one slot per *user*, shared across every
topic. Starting a browse in one topic silently clobbered another topic's
in-progress browse and discarded its pending first message. These tests
pin down that ``get_browse_state`` / ``set_browse_state`` /
``clear_browse_state`` key everything by thread_id so concurrent topics
never see or touch each other's state.
"""

from ccbot.handlers.directory_browser import (
    BROWSE_BY_THREAD_KEY,
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    PENDING_TEXT_KEY,
    SELECTED_PATH_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    UNBOUND_WINDOWS_KEY,
    clear_browse_state,
    get_browse_state,
    set_browse_state,
)


class TestGetBrowseState:
    def test_missing_thread_returns_empty_dict(self):
        assert get_browse_state({}, 42) == {}

    def test_none_user_data_returns_empty_dict(self):
        assert get_browse_state(None, 42) == {}

    def test_returns_set_fields(self):
        user_data: dict = {}
        set_browse_state(user_data, 42, {STATE_KEY: STATE_BROWSING_DIRECTORY})
        assert get_browse_state(user_data, 42) == {STATE_KEY: STATE_BROWSING_DIRECTORY}


class TestSetBrowseState:
    def test_none_user_data_is_a_noop(self):
        # Must not raise.
        set_browse_state(None, 42, {STATE_KEY: STATE_BROWSING_DIRECTORY})

    def test_merges_rather_than_replaces(self):
        user_data: dict = {}
        set_browse_state(user_data, 42, {STATE_KEY: STATE_BROWSING_DIRECTORY})
        set_browse_state(user_data, 42, {PENDING_TEXT_KEY: "hello"})
        assert get_browse_state(user_data, 42) == {
            STATE_KEY: STATE_BROWSING_DIRECTORY,
            PENDING_TEXT_KEY: "hello",
        }

    def test_overwrites_only_named_fields(self):
        user_data: dict = {}
        set_browse_state(
            user_data,
            42,
            {BROWSE_PATH_KEY: "/a", BROWSE_PAGE_KEY: 0, PENDING_TEXT_KEY: "hello"},
        )
        set_browse_state(user_data, 42, {BROWSE_PATH_KEY: "/a/b", BROWSE_PAGE_KEY: 1})
        state = get_browse_state(user_data, 42)
        assert state[BROWSE_PATH_KEY] == "/a/b"
        assert state[BROWSE_PAGE_KEY] == 1
        # Untouched field (the pending first message) survives the transition.
        assert state[PENDING_TEXT_KEY] == "hello"


class TestClearBrowseState:
    def test_none_user_data_is_a_noop(self):
        clear_browse_state(None, 42)

    def test_drops_entire_entry(self):
        user_data: dict = {}
        set_browse_state(
            user_data, 42, {STATE_KEY: STATE_BROWSING_DIRECTORY, PENDING_TEXT_KEY: "hi"}
        )
        clear_browse_state(user_data, 42)
        assert get_browse_state(user_data, 42) == {}

    def test_missing_thread_is_a_noop(self):
        user_data: dict = {BROWSE_BY_THREAD_KEY: {}}
        clear_browse_state(user_data, 999)  # must not raise
        assert user_data == {BROWSE_BY_THREAD_KEY: {}}


class TestTwoThreadsDoNotClobberEachOther:
    """The regression this whole helper module exists to prevent: two
    topics (thread 42 and thread 43) browsing concurrently must never see
    or discard each other's state or pending message."""

    def test_starting_a_browse_in_one_thread_leaves_the_other_untouched(self):
        user_data: dict = {}

        # Topic 42 starts browsing with a pending first message.
        set_browse_state(
            user_data,
            42,
            {
                STATE_KEY: STATE_BROWSING_DIRECTORY,
                BROWSE_PATH_KEY: "/home/alice",
                BROWSE_PAGE_KEY: 0,
                BROWSE_DIRS_KEY: ["projects", "notes"],
                PENDING_TEXT_KEY: "hello from topic 42",
            },
        )

        # Topic 43 starts its own, independent browse with a different
        # pending message — this used to stomp on topic 42's single global
        # slot (RC13 / f31 / f33).
        set_browse_state(
            user_data,
            43,
            {
                STATE_KEY: STATE_BROWSING_DIRECTORY,
                BROWSE_PATH_KEY: "/home/bob",
                BROWSE_PAGE_KEY: 0,
                BROWSE_DIRS_KEY: ["work"],
                PENDING_TEXT_KEY: "hello from topic 43",
            },
        )

        state_42 = get_browse_state(user_data, 42)
        state_43 = get_browse_state(user_data, 43)

        assert state_42[BROWSE_PATH_KEY] == "/home/alice"
        assert state_42[PENDING_TEXT_KEY] == "hello from topic 42"
        assert state_43[BROWSE_PATH_KEY] == "/home/bob"
        assert state_43[PENDING_TEXT_KEY] == "hello from topic 43"

    def test_completing_one_thread_does_not_affect_the_other(self):
        user_data: dict = {}
        set_browse_state(
            user_data,
            42,
            {
                STATE_KEY: STATE_BROWSING_DIRECTORY,
                BROWSE_PATH_KEY: "/home/alice",
                PENDING_TEXT_KEY: "hello from topic 42",
            },
        )
        set_browse_state(
            user_data,
            43,
            {
                STATE_KEY: STATE_BROWSING_DIRECTORY,
                BROWSE_PATH_KEY: "/home/bob",
                PENDING_TEXT_KEY: "hello from topic 43",
            },
        )

        # Topic 42 confirms its directory and transitions to the session
        # picker sub-flow (mirrors CB_DIR_CONFIRM in bot.py).
        set_browse_state(
            user_data,
            42,
            {
                STATE_KEY: STATE_SELECTING_SESSION,
                SESSIONS_KEY: ["session-a"],
                SELECTED_PATH_KEY: "/home/alice",
            },
        )

        # Topic 43's independent browse is completely unaffected.
        state_43 = get_browse_state(user_data, 43)
        assert state_43[STATE_KEY] == STATE_BROWSING_DIRECTORY
        assert state_43[BROWSE_PATH_KEY] == "/home/bob"
        assert state_43[PENDING_TEXT_KEY] == "hello from topic 43"

        # And topic 42 kept its pending text across the sub-flow transition.
        state_42 = get_browse_state(user_data, 42)
        assert state_42[STATE_KEY] == STATE_SELECTING_SESSION
        assert state_42[PENDING_TEXT_KEY] == "hello from topic 42"

        # Topic 42 finishes (mirrors _create_and_bind_window's final clear).
        clear_browse_state(user_data, 42)
        assert get_browse_state(user_data, 42) == {}

        # Topic 43 is still untouched by topic 42's completion.
        state_43_after = get_browse_state(user_data, 43)
        assert state_43_after[BROWSE_PATH_KEY] == "/home/bob"
        assert state_43_after[PENDING_TEXT_KEY] == "hello from topic 43"

    def test_cancelling_one_thread_does_not_affect_the_other(self):
        user_data: dict = {}
        set_browse_state(
            user_data,
            42,
            {
                STATE_KEY: STATE_BROWSING_DIRECTORY,
                UNBOUND_WINDOWS_KEY: ["@0"],
                PENDING_TEXT_KEY: "hello from topic 42",
            },
        )
        set_browse_state(
            user_data,
            43,
            {
                STATE_KEY: STATE_BROWSING_DIRECTORY,
                UNBOUND_WINDOWS_KEY: ["@1"],
                PENDING_TEXT_KEY: "hello from topic 43",
            },
        )

        # Topic 42 cancels (mirrors CB_DIR_CANCEL / CB_WIN_CANCEL).
        clear_browse_state(user_data, 42)

        assert get_browse_state(user_data, 42) == {}
        state_43 = get_browse_state(user_data, 43)
        assert state_43[UNBOUND_WINDOWS_KEY] == ["@1"]
        assert state_43[PENDING_TEXT_KEY] == "hello from topic 43"
