/*
    Fundamental Web App - Market tables
    Version: 1
    Purpose: Create separate SQL Server tables for local market-symbol metadata
             and daily market snapshots.

    SAFETY:
      - This script does not insert, update, or delete data.
      - It does not modify existing fundamental-analysis tables.
      - Review database/schema before execution.

    Design note:
      Market snapshot fields are stored as NVARCHAR to preserve the exact source
      text from SQLite during the initial migration. Typed analytics columns can
      be added only after validating the actual source formats and decimal rules.
*/
SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

IF OBJECT_ID(N'dbo.MarketSymbols', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.MarketSymbols
    (
        MarketSymbolID                 BIGINT IDENTITY(1,1) NOT NULL,
        LocalSymbolID                  BIGINT NULL,
        InsCode                        NVARCHAR(100) NOT NULL,
        Symbol                         NVARCHAR(100) NOT NULL,
        CompanyName                    NVARCHAR(300) NOT NULL CONSTRAINT DF_MarketSymbols_CompanyName DEFAULT (N''),
        ISIN                           NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketSymbols_ISIN DEFAULT (N''),
        MarketBoard                    NVARCHAR(150) NOT NULL CONSTRAINT DF_MarketSymbols_MarketBoard DEFAULT (N'نامشخص'),
        Flow                           NVARCHAR(150) NOT NULL CONSTRAINT DF_MarketSymbols_Flow DEFAULT (N''),
        RawJson                        NVARCHAR(MAX) NOT NULL CONSTRAINT DF_MarketSymbols_RawJson DEFAULT (N'{}'),
        IsActive                       BIT NOT NULL CONSTRAINT DF_MarketSymbols_IsActive DEFAULT (1),
        LastSyncedAt                   DATETIME2(0) NULL,
        UpdatedAt                      DATETIME2(0) NULL,
        InstrumentCodeIsPlaceholder    BIT NOT NULL CONSTRAINT DF_MarketSymbols_CodePlaceholder DEFAULT (0),
        CreatedAt                      DATETIME2(0) NOT NULL CONSTRAINT DF_MarketSymbols_CreatedAt DEFAULT (SYSDATETIME()),
        CONSTRAINT PK_MarketSymbols PRIMARY KEY CLUSTERED (MarketSymbolID),
        CONSTRAINT UQ_MarketSymbols_InsCode UNIQUE (InsCode)
    );
END;
GO

IF NOT EXISTS
(
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'dbo.MarketSymbols')
      AND name = N'IX_MarketSymbols_Symbol'
)
    CREATE INDEX IX_MarketSymbols_Symbol ON dbo.MarketSymbols (Symbol);
GO

IF NOT EXISTS
(
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'dbo.MarketSymbols')
      AND name = N'IX_MarketSymbols_CompanyName'
)
    CREATE INDEX IX_MarketSymbols_CompanyName ON dbo.MarketSymbols (CompanyName);
GO

IF OBJECT_ID(N'dbo.MarketDailyData', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.MarketDailyData
    (
        MarketDataID       BIGINT IDENTITY(1,1) NOT NULL,
        LocalMarketDataID  BIGINT NULL,
        InsCode            NVARCHAR(100) NOT NULL,
        Symbol             NVARCHAR(100) NOT NULL,
        FetchedAt           DATETIME2(0) NOT NULL,
        TradeCount          NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_TradeCount DEFAULT (N''),
        TradeVolume         NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_TradeVolume DEFAULT (N''),
        TradeValue          NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_TradeValue DEFAULT (N''),
        YesterdayPrice      NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_YesterdayPrice DEFAULT (N''),
        FirstPrice          NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_FirstPrice DEFAULT (N''),
        LastTradePrice      NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_LastTradePrice DEFAULT (N''),
        LastTradeChange     NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_LastTradeChange DEFAULT (N''),
        LastTradePercent    NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_LastTradePercent DEFAULT (N''),
        ClosingPrice        NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_ClosingPrice DEFAULT (N''),
        ClosingChange       NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_ClosingChange DEFAULT (N''),
        ClosingPercent      NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_ClosingPercent DEFAULT (N''),
        LowPrice            NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_LowPrice DEFAULT (N''),
        HighPrice           NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_HighPrice DEFAULT (N''),
        EPS                 NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_EPS DEFAULT (N''),
        PERatio             NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_PERatio DEFAULT (N''),
        BuyCount            NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_BuyCount DEFAULT (N''),
        BuyVolume           NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_BuyVolume DEFAULT (N''),
        BuyPrice            NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_BuyPrice DEFAULT (N''),
        SellPrice           NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_SellPrice DEFAULT (N''),
        SellVolume          NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_SellVolume DEFAULT (N''),
        SellCount           NVARCHAR(100) NOT NULL CONSTRAINT DF_MarketDailyData_SellCount DEFAULT (N''),
        Source              NVARCHAR(200) NOT NULL CONSTRAINT DF_MarketDailyData_Source DEFAULT (N'TSETMC MarketWatchPlus'),
        ImportedAt          DATETIME2(0) NOT NULL CONSTRAINT DF_MarketDailyData_ImportedAt DEFAULT (SYSDATETIME()),
        CONSTRAINT PK_MarketDailyData PRIMARY KEY CLUSTERED (MarketDataID),
        CONSTRAINT FK_MarketDailyData_MarketSymbols_InsCode
            FOREIGN KEY (InsCode) REFERENCES dbo.MarketSymbols (InsCode)
    );
END;
GO

IF NOT EXISTS
(
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'dbo.MarketDailyData')
      AND name = N'IX_MarketDailyData_InsCode_FetchedAt'
)
    CREATE INDEX IX_MarketDailyData_InsCode_FetchedAt
        ON dbo.MarketDailyData (InsCode, FetchedAt DESC);
GO

/*
    Initial migration idempotency rule:
    The application will use LocalMarketDataID to prevent re-importing the same
    SQLite row. A unique index is intentionally deferred until existing rows
    and any prior manual imports have been checked.
*/
PRINT N'Schema check complete. Existing tables were left unchanged; missing market tables/indexes were created.';
GO
