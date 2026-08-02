from services.logger_service import LoggerService


def test_close_releases_log_file_for_immediate_cleanup(tmp_path):
    log_path = tmp_path / "logs" / "test.log"
    logger = LoggerService(log_path)
    logger.info("release check")

    logger.close()
    log_path.unlink()

    assert not log_path.exists()
