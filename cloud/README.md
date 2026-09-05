# MedStudy 클라우드 학습 앱

이 앱은 Mac이 꺼져 있어도 정리본 PDF를 읽고 객관식 문제를 풀기 위한
학습 전용 화면입니다. AI 생성과 원본 업로드 기능은 의도적으로 포함하지 않습니다.

## 최초 설정

1. Supabase 프로젝트를 만들고 SQL Editor에서 `schema.sql`을 실행합니다.
2. Storage에서 `medstudy`라는 **private bucket**을 만듭니다.
3. 프로젝트 루트에 `.streamlit/secrets.toml`을 만들고
   `secrets.example.toml`의 값을 채웁니다. 이 파일은 Git에 포함되지 않습니다.
4. Mac 가상환경에 `pip install "supabase>=2.18,<3"`를 설치합니다.
5. 프로젝트 루트에서 `.venv/bin/python cloud_sync.py`를 한 번 실행합니다.
6. 이 폴더가 포함된 GitHub 저장소를 Streamlit Community Cloud에 연결하고
   entrypoint를 `cloud/cloud_app.py`로 지정합니다.
7. Community Cloud의 Secrets에도 같은 내용을 붙여 넣습니다.

설정 후에는 새 정리본 생성이 끝날 때 해당 강의가 자동으로 동기화됩니다.
추가 문제만 만든 뒤에는 `.venv/bin/python cloud_sync.py`를 실행하면 갱신됩니다.
