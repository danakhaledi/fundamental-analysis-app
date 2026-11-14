/*
    TSETMC official company/industry identity links.
    Additive and idempotent. No data is inserted, updated, or deleted.
    Run against the fundamental-analysis SQL Server database.
*/
SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

IF OBJECT_ID(N'dbo.Companies', N'U') IS NULL
    THROW 51100, N'dbo.Companies does not exist.', 1;
IF OBJECT_ID(N'dbo.MarketSymbols', N'U') IS NULL
    THROW 51101, N'dbo.MarketSymbols does not exist.', 1;
GO

IF COL_LENGTH(N'dbo.Companies', N'TSETMCCompanyCode') IS NULL
    EXEC sys.sp_executesql N'ALTER TABLE dbo.Companies ADD TSETMCCompanyCode NVARCHAR(50) NULL;';
IF COL_LENGTH(N'dbo.Companies', N'CompanyISIN') IS NULL
    EXEC sys.sp_executesql N'ALTER TABLE dbo.Companies ADD CompanyISIN NVARCHAR(20) NULL;';
IF COL_LENGTH(N'dbo.MarketSymbols', N'CompanyID') IS NULL
    EXEC sys.sp_executesql N'ALTER TABLE dbo.MarketSymbols ADD CompanyID INT NULL;';
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id=OBJECT_ID(N'dbo.Companies')
      AND name=N'UX_Companies_TSETMCCompanyCode'
)
    CREATE UNIQUE INDEX UX_Companies_TSETMCCompanyCode
        ON dbo.Companies(TSETMCCompanyCode)
        WHERE TSETMCCompanyCode IS NOT NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id=OBJECT_ID(N'dbo.Companies')
      AND name=N'UX_Companies_CompanyISIN'
)
    CREATE UNIQUE INDEX UX_Companies_CompanyISIN
        ON dbo.Companies(CompanyISIN)
        WHERE CompanyISIN IS NOT NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.foreign_keys
    WHERE parent_object_id=OBJECT_ID(N'dbo.MarketSymbols')
      AND name=N'FK_MarketSymbols_Companies'
)
    ALTER TABLE dbo.MarketSymbols WITH CHECK
        ADD CONSTRAINT FK_MarketSymbols_Companies
        FOREIGN KEY(CompanyID) REFERENCES dbo.Companies(CompanyID);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id=OBJECT_ID(N'dbo.MarketSymbols')
      AND name=N'IX_MarketSymbols_CompanyID'
)
    CREATE INDEX IX_MarketSymbols_CompanyID
        ON dbo.MarketSymbols(CompanyID);
GO

PRINT N'TSETMC company/industry identity schema is ready. No data rows were modified.';
GO
