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
    SELECT 1
    FROM sys.indexes i
    INNER JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id
    INNER JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id
    WHERE i.object_id=OBJECT_ID(N'dbo.Companies')
      AND i.is_unique=1 AND i.has_filter=1
      AND c.name=N'TSETMCCompanyCode' AND ic.key_ordinal=1
)
    CREATE UNIQUE INDEX UX_Companies_TSETMCCompanyCode
        ON dbo.Companies(TSETMCCompanyCode)
        WHERE TSETMCCompanyCode IS NOT NULL;
GO

IF NOT EXISTS (
    SELECT 1
    FROM sys.indexes i
    INNER JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id
    INNER JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id
    WHERE i.object_id=OBJECT_ID(N'dbo.Companies')
      AND i.is_unique=1 AND i.has_filter=1
      AND c.name=N'CompanyISIN' AND ic.key_ordinal=1
)
    CREATE UNIQUE INDEX UX_Companies_CompanyISIN
        ON dbo.Companies(CompanyISIN)
        WHERE CompanyISIN IS NOT NULL;
GO

IF NOT EXISTS (
    SELECT 1
    FROM sys.foreign_key_columns fkc
    INNER JOIN sys.columns pc ON pc.object_id=fkc.parent_object_id AND pc.column_id=fkc.parent_column_id
    INNER JOIN sys.columns rc ON rc.object_id=fkc.referenced_object_id AND rc.column_id=fkc.referenced_column_id
    WHERE fkc.parent_object_id=OBJECT_ID(N'dbo.MarketSymbols')
      AND fkc.referenced_object_id=OBJECT_ID(N'dbo.Companies')
      AND pc.name=N'CompanyID' AND rc.name=N'CompanyID'
)
    ALTER TABLE dbo.MarketSymbols WITH CHECK
        ADD CONSTRAINT FK_MarketSymbols_Companies
        FOREIGN KEY(CompanyID) REFERENCES dbo.Companies(CompanyID);
GO

IF NOT EXISTS (
    SELECT 1
    FROM sys.indexes i
    INNER JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id
    INNER JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id
    WHERE i.object_id=OBJECT_ID(N'dbo.MarketSymbols')
      AND c.name=N'CompanyID' AND ic.key_ordinal=1
)
    CREATE INDEX IX_MarketSymbols_CompanyID
        ON dbo.MarketSymbols(CompanyID);
GO

PRINT N'TSETMC company/industry identity schema is ready. No data rows were modified.';
GO
