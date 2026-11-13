/* Additive migration for an existing dbo.MarketDailyData created by v13.
   Safe to run repeatedly. Existing snapshot rows remain unchanged; TradeDate stays NULL. */
IF COL_LENGTH(N'dbo.MarketDailyData', N'TradeDate') IS NULL
BEGIN
    ALTER TABLE dbo.MarketDailyData ADD TradeDate DATE NULL;
END;
GO

IF NOT EXISTS
(
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'dbo.MarketDailyData')
      AND name = N'UX_MarketDailyData_InsCode_TradeDate'
)
BEGIN
    CREATE UNIQUE INDEX UX_MarketDailyData_InsCode_TradeDate
        ON dbo.MarketDailyData (InsCode, TradeDate)
        WHERE TradeDate IS NOT NULL;
END;
GO
