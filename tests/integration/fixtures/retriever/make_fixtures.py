"""Generate synthetic Korean PDF fixtures for the P1-003A NeMo Retriever live smoke.

All content is fictional. Run: uv run --no-project --with reportlab==4.4.4 python make_fixtures.py
"""

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

FONT = "HYSMyeongJo-Medium"
OUT = Path(__file__).parent

DOCUMENTS = {
    "synthetic_alpha_minutes.pdf": [
        [
            "합성 프로젝트 알파 회의록 (가상 자료)",
            "1쪽: 개요",
            "프로젝트 알파는 사내 문서 검색 비서를 만드는 가상 프로젝트다.",
            "이번 회의는 2026년 10월 2일에 열렸고 여섯 명이 참석했다.",
        ],
        [
            "2쪽: 일정 결정",
            "베타 출시일은 11월 14일로 확정했다.",
            "베타 출시 담당자는 김하늘이다.",
            "정식 출시는 베타 피드백을 반영한 뒤 12월에 다시 논의한다.",
        ],
        [
            "3쪽: 예산",
            "3분기 GPU 예산은 1200만 원으로 승인되었다.",
            "추가 예산 요청은 재무팀 검토 후 결정한다.",
        ],
    ],
    "synthetic_security_policy.pdf": [
        [
            "합성 데이터 보안 정책 (가상 자료)",
            "1쪽: 적용 범위",
            "이 정책은 가상 회사의 사내 문서와 회의록에 적용된다.",
        ],
        [
            "2쪽: 보관 기간",
            "개인정보가 포함된 회의 녹음 파일은 90일 동안 보관한 뒤 삭제한다.",
            "외부 공유가 필요하면 보안 담당자의 사전 승인을 받아야 한다.",
        ],
    ],
}


def main() -> None:
    pdfmetrics.registerFont(UnicodeCIDFont(FONT))
    for name, pages in DOCUMENTS.items():
        pdf = canvas.Canvas(str(OUT / name), pagesize=A4, invariant=1)
        pdf.setTitle(name.removesuffix(".pdf"))
        for lines in pages:
            pdf.setFont(FONT, 13)
            y = 780
            for line in lines:
                pdf.drawString(60, y, line)
                y -= 26
            pdf.showPage()
        pdf.save()


if __name__ == "__main__":
    main()
