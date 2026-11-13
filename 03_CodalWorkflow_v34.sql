/* v34.5 - رفع ترتیب کامپایل ستون TracingNo و ایندکس Stage عمومی محصولات/مواد
   روی همان دیتابیس SQL Server اجرا شود. اسکریپت idempotent است. */
SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF OBJECT_ID(N'dbo.CodalReportWorkflow',N'U') IS NULL
    THROW 51000, N'dbo.CodalReportWorkflow وجود ندارد؛ ابتدا اسکریپت 00_CreateCodalWorkflow.sql را اجرا کنید.', 1;

IF COL_LENGTH('dbo.CodalReportWorkflow','ProductStatus') IS NULL
    ALTER TABLE dbo.CodalReportWorkflow ADD ProductStatus VARCHAR(20) NOT NULL
        CONSTRAINT DF_CodalReportWorkflow_ProductStatus DEFAULT('PENDING');
IF COL_LENGTH('dbo.CodalReportWorkflow','ProductResultJson') IS NULL
    ALTER TABLE dbo.CodalReportWorkflow ADD ProductResultJson NVARCHAR(MAX) NULL;
IF COL_LENGTH('dbo.CodalReportWorkflow','ProductError') IS NULL
    ALTER TABLE dbo.CodalReportWorkflow ADD ProductError NVARCHAR(4000) NULL;
IF COL_LENGTH('dbo.CodalReportWorkflow','ProductProcessedAt') IS NULL
    ALTER TABLE dbo.CodalReportWorkflow ADD ProductProcessedAt DATETIME2 NULL;

IF COL_LENGTH('dbo.CodalReportWorkflow','SalesStatus') IS NULL
    ALTER TABLE dbo.CodalReportWorkflow ADD SalesStatus VARCHAR(20) NOT NULL
        CONSTRAINT DF_CodalReportWorkflow_SalesStatus DEFAULT('PENDING');
IF COL_LENGTH('dbo.CodalReportWorkflow','SalesResultJson') IS NULL
    ALTER TABLE dbo.CodalReportWorkflow ADD SalesResultJson NVARCHAR(MAX) NULL;
IF COL_LENGTH('dbo.CodalReportWorkflow','SalesError') IS NULL
    ALTER TABLE dbo.CodalReportWorkflow ADD SalesError NVARCHAR(4000) NULL;
IF COL_LENGTH('dbo.CodalReportWorkflow','SalesProcessedAt') IS NULL
    ALTER TABLE dbo.CodalReportWorkflow ADD SalesProcessedAt DATETIME2 NULL;

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE parent_object_id=OBJECT_ID('dbo.CodalReportWorkflow') AND definition LIKE '%ProductStatus%')
    ALTER TABLE dbo.CodalReportWorkflow ADD CONSTRAINT CK_CodalReportWorkflow_ProductStatus
        CHECK (ProductStatus IN ('PENDING','SUCCESS','FAILED'));
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE parent_object_id=OBJECT_ID('dbo.CodalReportWorkflow') AND definition LIKE '%SalesStatus%')
    ALTER TABLE dbo.CodalReportWorkflow ADD CONSTRAINT CK_CodalReportWorkflow_SalesStatus
        CHECK (SalesStatus IN ('PENDING','SUCCESS','FAILED'));

IF OBJECT_ID(N'dbo.CodalProductStage',N'U') IS NULL
BEGIN
    CREATE TABLE dbo.CodalProductStage
    (
        ProductStageID BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_CodalProductStage PRIMARY KEY,
        SourceDocumentID BIGINT NOT NULL,
        CompanyID INT NOT NULL,
        ProductNameRaw NVARCHAR(400) NOT NULL,
        SourceTable VARCHAR(30) NOT NULL,
        SourceRowNo INT NULL,
        ItemType VARCHAR(30) NOT NULL,
        Unit VARCHAR(30) NULL,
        ProductID INT NULL,
        ValidationStatus VARCHAR(20) NOT NULL,
        ValidationMessage NVARCHAR(1000) NULL,
        CreatedAt DATETIME2 NOT NULL CONSTRAINT DF_CodalProductStage_CreatedAt DEFAULT(SYSDATETIME()),
        CONSTRAINT FK_CodalProductStage_SourceDocuments FOREIGN KEY(SourceDocumentID) REFERENCES dbo.SourceDocuments(SourceDocumentID),
        CONSTRAINT FK_CodalProductStage_Companies FOREIGN KEY(CompanyID) REFERENCES dbo.Companies(CompanyID),
        CONSTRAINT FK_CodalProductStage_Products FOREIGN KEY(ProductID) REFERENCES dbo.Products(ProductID),
        CONSTRAINT CK_CodalProductStage_Status CHECK(ValidationStatus IN ('VALID','ERROR')),
        CONSTRAINT CK_CodalProductStage_ItemType CHECK(ItemType IN ('PRODUCT','PURCHASE_MATERIAL','CONSUMED_RESOURCE'))
    );
    CREATE INDEX IX_CodalProductStage_Document_Status
        ON dbo.CodalProductStage(SourceDocumentID,ValidationStatus);
    CREATE INDEX IX_CodalProductStage_Product
        ON dbo.CodalProductStage(ProductID);
END;

/* v34.3: make product discovery stage global, not company/document dependent.
   Existing rows are retained; only the two legacy foreign keys are removed. */
IF OBJECT_ID(N'dbo.CodalProductStage',N'U') IS NOT NULL
BEGIN
    IF EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name=N'FK_CodalProductStage_SourceDocuments' AND parent_object_id=OBJECT_ID(N'dbo.CodalProductStage'))
        ALTER TABLE dbo.CodalProductStage DROP CONSTRAINT FK_CodalProductStage_SourceDocuments;
    IF EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name=N'FK_CodalProductStage_Companies' AND parent_object_id=OBJECT_ID(N'dbo.CodalProductStage'))
        ALTER TABLE dbo.CodalProductStage DROP CONSTRAINT FK_CodalProductStage_Companies;

    IF COL_LENGTH('dbo.CodalProductStage','SourceDocumentID') IS NOT NULL
        ALTER TABLE dbo.CodalProductStage ALTER COLUMN SourceDocumentID BIGINT NULL;
    IF COL_LENGTH('dbo.CodalProductStage','CompanyID') IS NOT NULL
        ALTER TABLE dbo.CodalProductStage ALTER COLUMN CompanyID INT NULL;

    IF COL_LENGTH('dbo.CodalProductStage','Symbol') IS NULL
        ALTER TABLE dbo.CodalProductStage ADD Symbol NVARCHAR(100) NULL;
    IF COL_LENGTH('dbo.CodalProductStage','TracingNo') IS NULL
        ALTER TABLE dbo.CodalProductStage ADD TracingNo NVARCHAR(50) NULL;
    IF COL_LENGTH('dbo.CodalProductStage','ReportTitle') IS NULL
        ALTER TABLE dbo.CodalProductStage ADD ReportTitle NVARCHAR(500) NULL;

    IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name=N'CK_CodalProductStage_Status' AND parent_object_id=OBJECT_ID(N'dbo.CodalProductStage'))
        ALTER TABLE dbo.CodalProductStage DROP CONSTRAINT CK_CodalProductStage_Status;
    ALTER TABLE dbo.CodalProductStage WITH CHECK ADD CONSTRAINT CK_CodalProductStage_Status
        CHECK (ValidationStatus IN ('PENDING','VALID','ERROR'));

    IF COL_LENGTH(N'dbo.CodalProductStage', N'TracingNo') IS NOT NULL
       AND NOT EXISTS (
            SELECT 1 FROM sys.indexes
            WHERE object_id=OBJECT_ID(N'dbo.CodalProductStage')
              AND name=N'UX_CodalProductStage_Trace_Row'
       )
    BEGIN
        /* Dynamic SQL defers name resolution until after the ALTER TABLE above. */
        EXEC sys.sp_executesql N'
            CREATE UNIQUE INDEX UX_CodalProductStage_Trace_Row
            ON dbo.CodalProductStage(TracingNo,SourceTable,SourceRowNo)
            WHERE TracingNo IS NOT NULL AND SourceRowNo IS NOT NULL;';
    END;
END;

/* v33 marked resource staging as SUCCESS without transferring to the final table.
   Reset that legacy status so v34 can perform the first verified final-table upsert.
   The transfer is upsert-based and does not delete existing resource records. */
IF OBJECT_ID(N'dbo.CodalWorkflowMeta',N'U') IS NULL
    THROW 51001, N'dbo.CodalWorkflowMeta وجود ندارد؛ ابتدا اسکریپت 00_CreateCodalWorkflow.sql را اجرا کنید.', 1;

IF NOT EXISTS (SELECT 1 FROM dbo.CodalWorkflowMeta WHERE MetaKey=N'v34_resource_transfer_reset')
BEGIN
    UPDATE dbo.CodalReportWorkflow
    SET ResourceStatus='PENDING', ResourceError=NULL, ResourceResultJson=NULL, ResourceProcessedAt=NULL,
        UpdatedAt=SYSDATETIME()
    WHERE ResourceStatus='SUCCESS';

    INSERT dbo.CodalWorkflowMeta(MetaKey,MetaValue)
    VALUES (N'v34_resource_transfer_reset', CONVERT(NVARCHAR(30),SYSDATETIME(),126));
END;

/* v34.3: successful product runs from v34.2 did not populate the generic stage.
   Reset them once so the new idempotent stage-and-catalog flow can backfill every report. */
IF NOT EXISTS (SELECT 1 FROM dbo.CodalWorkflowMeta WHERE MetaKey=N'v34_3_product_stage_reset')
BEGIN
    UPDATE dbo.CodalReportWorkflow
    SET ProductStatus='PENDING', ProductError=NULL, ProductResultJson=NULL, ProductProcessedAt=NULL,
        UpdatedAt=SYSDATETIME()
    WHERE ProductStatus='SUCCESS';

    INSERT dbo.CodalWorkflowMeta(MetaKey,MetaValue)
    VALUES (N'v34_3_product_stage_reset', CONVERT(NVARCHAR(30),SYSDATETIME(),126));
END;

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.CodalReportWorkflow') AND name='IX_CodalReportWorkflow_ProductSalesStatus')
    CREATE INDEX IX_CodalReportWorkflow_ProductSalesStatus
        ON dbo.CodalReportWorkflow(ProductStatus,SalesStatus,ResourceStatus,DiscoveredAt DESC);

COMMIT TRANSACTION;
GO
