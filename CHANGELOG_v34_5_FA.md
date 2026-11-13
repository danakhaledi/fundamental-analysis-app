# نسخه v34.5

## رفع خطای Invalid column name 'TracingNo'

علت خطا، کامپایل ایندکس `UX_CodalProductStage_Trace_Row` در همان batch حاوی `ALTER TABLE ... ADD TracingNo` بود. SQL Server ممکن است ارجاع به ستون جدید را پیش از اجرای ALTER بررسی کند.

اصلاح در هر دو مسیر انجام شد:
- اسکریپت `03_CodalWorkflow_v34.sql`
- مهاجرت خودکار زمان اجرای برنامه در `app/codal_products_stage.py`

ایجاد ایندکس اکنون با `sys.sp_executesql` و پس از بررسی وجود ستون انجام می‌شود تا نام ستون در زمان اجرای دستور پویا resolve شود.

هیچ ستون یا داده‌ای حذف نمی‌شود. این اصلاح فقط ترتیب/زمان کامپایل ایجاد ایندکس را ایمن می‌کند.
