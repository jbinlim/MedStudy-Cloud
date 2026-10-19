create table if not exists public.medstudy_subjects (
  id bigint primary key,
  name text not null
);

create table if not exists public.medstudy_lectures (
  id bigint primary key,
  subject_id bigint not null references public.medstudy_subjects(id) on delete cascade,
  lecture_no integer not null,
  title text not null default '',
  summary_pdf_path text,
  exam_pdf_path text,
  summary_updated_at timestamptz,
  unique(subject_id, lecture_no)
);

-- 기존에 만든 테이블에는 이 컬럼을 추가로 실행하세요:
-- alter table public.medstudy_lectures add column if not exists exam_pdf_path text;

create table if not exists public.medstudy_quiz_sets (
  lecture_id bigint not null references public.medstudy_lectures(id) on delete cascade,
  set_no integer not null,
  content text not null,
  updated_at timestamptz not null default now(),
  primary key(lecture_id, set_no)
);

create table if not exists public.medstudy_quiz_progress (
  lecture_id bigint not null references public.medstudy_lectures(id) on delete cascade,
  question_key text not null,
  question text not null,
  answer text not null,
  incorrect_count integer not null default 0,
  correct_count integer not null default 0,
  review_status text not null default 'unseen',
  last_result text,
  repetitions integer not null default 0,
  ease_factor real not null default 2.5,
  interval_days integer not null default 0,
  due_date date,
  updated_at timestamptz not null default now(),
  primary key(lecture_id, question_key)
);

-- 기존에 만든 테이블에는 이 컬럼들을 추가로 실행하세요(SM-2 간격 반복 스케줄링):
-- alter table public.medstudy_quiz_progress add column if not exists repetitions integer not null default 0;
-- alter table public.medstudy_quiz_progress add column if not exists ease_factor real not null default 2.5;
-- alter table public.medstudy_quiz_progress add column if not exists interval_days integer not null default 0;
-- alter table public.medstudy_quiz_progress add column if not exists due_date date;

-- 위 컬럼들이 생기기 전부터 있던 행(이미 풀었던 문제)은 due_date가
-- 비어 있어 "오늘 복습" 큐에 전혀 뜨지 않습니다. 아래를 한 번 실행하면
-- 그런 문제를 지금부터 바로 복습 대상으로 올리고, 이후 채점부터는
-- SM-2 주기가 정상적으로 굴러갑니다:
-- update public.medstudy_quiz_progress set due_date = current_date, updated_at = now() where due_date is null;

alter table public.medstudy_subjects enable row level security;
alter table public.medstudy_lectures enable row level security;
alter table public.medstudy_quiz_sets enable row level security;
alter table public.medstudy_quiz_progress enable row level security;

-- 앱과 Mac 동기화기는 Streamlit Secrets에 보관한 service_role 키를 사용한다.
-- 브라우저로 키가 전달되지 않으므로 공개 RLS 정책은 만들지 않는다.
