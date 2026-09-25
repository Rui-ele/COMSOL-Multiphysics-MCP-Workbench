"""Basic tests for COMSOL MCP Server."""

class TestSessionManager:
    """Tests for session manager (without actual COMSOL)."""

    def test_session_manager_singleton(self):
        from src.core.session import SessionManager

        sm1 = SessionManager()
        sm2 = SessionManager()
        assert sm1 is sm2

    def test_session_manager_initial_state(self):
        from src.core.session import SessionManager

        sm = SessionManager()
        assert sm.client is None
        assert not sm.is_connected
        assert sm.current_model is None
        assert sm.models == {}

    def test_get_status_disconnected(self):
        from src.core.session import SessionManager

        sm = SessionManager()
        status = sm.get_status()
        assert status["connected"] is False
