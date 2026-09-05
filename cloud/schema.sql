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
  summary_updated_at timestamptz,
  unique(subject_id, lecture_no)
);

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
  updated_at timestamptz not null default now(),
  primary key(lecture_id, question_key)
);

alter table public.medstudy_subjects enable row level security;
alter table public.medstudy_lectures enable row level security;
alter table public.medstudy_quiz_sets enable row level security;
alter table public.medstudy_quiz_progress enable row level security;

-- 앱과 Mac 동기화기는 Streamlit Secrets에 보관한 service_role 키를 사용한다.
-- 브라우저로 키가 전달되지 않으므로 공개 RLS 정책은 만들지 않는다.
