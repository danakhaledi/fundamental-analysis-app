/* گردش کار پایدار کدال در پایگاه داده اصلی SQL Server */
IF OBJECT_ID(N'dbo.CodalReportWorkflow', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.CodalReportWorkflow
    (
        TracingNo NVARCHAR(50) NOT NULL CONSTRAINT PK_CodalReportWorkflow PRIMARY KEY,
        ReportJson NVARCHAR(MAX) NOT NULL,
        DiscoveredAt DATETIME2(0) NOT NULL CONSTRAINT DF_CodalReportWorkflow_DiscoveredAt DEFAULT (SYSDATETIME()),
        DownloadStatus VARCHAR(20) NOT NULL CONSTRAINT DF_CodalReportWorkflow_DownloadStatus DEFAULT ('PENDING'),
        DownloadFile NVARCHAR(500) NULL,
        DownloadError NVARCHAR(2000) NULL,
        DownloadedAt DATETIME2(0) NULL,
        ExtractStatus VARCHAR(20) NOT NULL CONSTRAINT DF_CodalReportWorkflow_ExtractStatus DEFAULT ('PENDING'),
        ExtractionJson NVARCHAR(MAX) NULL,
        ExtractError NVARCHAR(2000) NULL,
        ExtractedAt DATETIME2(0) NULL,
        ResourceStatus VARCHAR(20) NOT NULL CONSTRAINT DF_CodalReportWorkflow_ResourceStatus DEFAULT ('PENDING'),
        ResourceResultJson NVARCHAR(MAX) NULL,
        ResourceError NVARCHAR(2000) NULL,
        ResourceProcessedAt DATETIME2(0) NULL,
        UpdatedAt DATETIME2(0) NOT NULL CONSTRAINT DF_CodalReportWorkflow_UpdatedAt DEFAULT (SYSDATETIME()),
        CONSTRAINT CK_CodalReportWorkflow_DownloadStatus CHECK (DownloadStatus IN ('PENDING','SUCCESS','FAILED')),
        CONSTRAINT CK_CodalReportWorkflow_ExtractStatus CHECK (ExtractStatus IN ('PENDING','SUCCESS','FAILED')),
        CONSTRAINT CK_CodalReportWorkflow_ResourceStatus CHECK (ResourceStatus IN ('PENDING','SUCCESS','FAILED'))
    );
    CREATE INDEX IX_CodalReportWorkflow_Status
        ON dbo.CodalReportWorkflow(DownloadStatus, ExtractStatus, ResourceStatus, DiscoveredAt DESC);
END;
GO

IF OBJECT_ID(N'dbo.CodalWorkflowMeta', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.CodalWorkflowMeta
    (
        MetaKey NVARCHAR(100) NOT NULL CONSTRAINT PK_CodalWorkflowMeta PRIMARY KEY,
        MetaValue NVARCHAR(500) NOT NULL,
        UpdatedAt DATETIME2(0) NOT NULL CONSTRAINT DF_CodalWorkflowMeta_UpdatedAt DEFAULT (SYSDATETIME())
    );
END;
GO
