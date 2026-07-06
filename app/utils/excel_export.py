from io import BytesIO
from openpyxl import Workbook

class ExcelExport:

    @staticmethod
    def export_jd_list(records):

        wb = Workbook()

        ws = wb.active
        ws.title = "Job Descriptions"

        ws.append([
            "Title",
            "Source Format",
            "Version",
            "Jurisdiction",
            "Experience",
            "Education",
            "Created By",
            "Created At",
            "Status"
        ])

        for jd in records:

            ws.append([
                jd.title,
                jd.source_format.value,
                jd.version_number,
                jd.jurisdiction,
                float(jd.min_experience_years) if jd.min_experience_years else "",
                str(jd.education_criteria) if jd.education_criteria else "",
                jd.created_by,
                jd.created_at.strftime("%Y-%m-%d %H:%M"),
                "Active" if jd.is_active_version else "Closed"
            ])

        output = BytesIO()

        wb.save(output)

        output.seek(0)

        return output