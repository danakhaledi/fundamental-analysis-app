from app.appconfig import _with_long_as_max


def test_appends_long_as_max_when_missing():
    result = _with_long_as_max("DRIVER={ODBC Driver 18 for SQL Server};SERVER=.;DATABASE=FundamentalAnalysis")
    assert "LongAsMax=Yes" in result
    assert result.endswith(";")


def test_replaces_long_as_max_no_without_duplicate():
    result = _with_long_as_max("SERVER=.;LongAsMax=No;DATABASE=FundamentalAnalysis")
    assert result.count("LongAsMax=") == 1
    assert "LongAsMax=Yes" in result


def test_is_case_insensitive_for_existing_key():
    result = _with_long_as_max("SERVER=.;longasmax=Yes")
    assert result.count("LongAsMax=") == 1
