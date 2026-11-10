"use client";

/**
 * 지출 계획의 첨부파일 — 서버에 저장하고, 이 탭이 쓰는 목록을 여기에 둡니다.
 *
 * 올리는 즉시 서버로 보냅니다. 화면을 떠나도 올리기가 계속되고 결과가 같은 목록에 나타나도록
 * 목록을 컴포넌트 밖에 둡니다. 상세 화면과 수정 창이 `use첨부` 로 같은 목록을 봅니다.
 * 백엔드가 없는 예시 모드에서는 이 탭 안에서만 보관합니다.
 */
import { useSyncExternalStore } from "react";
import { API켜짐 } from "./config";
import { 계획상세, 첨부삭제, 첨부업로드, 첨부파일받기 } from "./api";
import type { 서버첨부 } from "./server-types";

export type 첨부 = {
  id: string;
  name: string;
  size: string;
  /** 증빙 항목에 올린 파일이면 그 항목 id. */
  taskId: string | null;
  /** 다른 구성원이 올렸으면 그 사람 이메일 앞부분. */
  작성자?: string | null;
  상태: "완료" | "올리는중" | "실패";
  /** 실패 이유. 화면에 그대로 보여 줍니다. */
  오류?: string;
  /** 이 탭에서 고른 파일. 다시 올리기와 예시 모드의 내려받기에 씁니다. */
  file?: File;
};

export type 첨부칸 = {
  목록: 첨부[];
  읽기: "대기" | "완료" | "실패";
  /** 올리기·지우기가 일어날 때마다 늘어납니다. 그 사이에 읽은 서버 목록은 버리고 다시 읽습니다. */
  세대: number;
};

export const 첨부한도 = { 개수: 10, 바이트: 3 * 1024 * 1024 };
export const 첨부확장자 = ["pdf", "hwp", "hwpx", "jpg", "jpeg", "png", "xlsx", "docx"];
export const 첨부accept = 첨부확장자.map((x) => `.${x}`).join(",");

const 빈칸: 첨부칸 = { 목록: [], 읽기: "대기", 세대: 0 };
const 보관함 = new Map<string, 첨부칸>();
const 듣는곳 = new Set<() => void>();

const 칸 = (planId: string) => 보관함.get(planId) ?? 빈칸;

function 바꾸기(planId: string, 변경: (c: 첨부칸) => 첨부칸) {
  보관함.set(planId, 변경(칸(planId)));
  듣는곳.forEach((f) => f());
}

function 듣기(f: () => void) {
  듣는곳.add(f);
  return () => {
    듣는곳.delete(f);
  };
}

export function use첨부(planId: string): 첨부칸 {
  return useSyncExternalStore(듣기, () => 칸(planId), () => 빈칸);
}

export function 크기표기(바이트: number): string {
  return 바이트 >= 1024 * 1024
    ? `${(바이트 / 1024 / 1024).toFixed(1)}MB`
    : `${Math.max(1, Math.round(바이트 / 1024))}KB`;
}

const 서버계획인가 = (planId: string) => API켜짐() && /^\d+$/.test(planId);

const 서버행을첨부로 = (a: 서버첨부): 첨부 => ({
  작성자: a.작성자 ?? null,
  id: a.attachment_id,
  name: a.원본파일명,
  size: 크기표기(a.크기),
  taskId: a.task_id == null ? null : String(a.task_id),
  상태: "완료",
});

/** 서버의 최신 목록을 읽습니다. 올리는 중이거나 실패한 줄은 이 탭에만 있으므로 남깁니다. */
export async function 첨부새로읽기(planId: string): Promise<void> {
  if (!서버계획인가(planId)) {
    바꾸기(planId, (c) => ({ ...c, 읽기: "완료" }));
    return;
  }
  try {
    let 세대: number;
    let 서버행: 첨부[];
    do {
      세대 = 칸(planId).세대;
      서버행 = ((await 계획상세(planId)).첨부 ?? []).map(서버행을첨부로);
    } while (세대 !== 칸(planId).세대);
    바꾸기(planId, (c) => ({
      ...c,
      읽기: "완료",
      목록: [...서버행, ...c.목록.filter((f) => f.상태 !== "완료")],
    }));
  } catch (e) {
    console.warn("[CHECKUMAIT] 첨부 목록을 읽지 못했습니다.", e);
    바꾸기(planId, (c) => ({ ...c, 읽기: "실패" }));
  }
}

function 실패문구(e: unknown): string {
  if ((e as { status?: number })?.status === 401) return "로그인하면 저장됩니다.";
  const 글 = e instanceof Error ? e.message : "";
  // 주의: 네트워크 오류의 영어 문구(Failed to fetch)를 화면에 내지 않습니다.
  return /[가-힣]/.test(글) ? 글 : "서버에 연결하지 못했습니다. 다시 올려 주세요.";
}

async function 줄_올리기(planId: string, 임시id: string, file: File, taskId: string | null): Promise<boolean> {
  try {
    const 서버행 = await 첨부업로드(planId, file, taskId && /^\d+$/.test(taskId) ? taskId : undefined);
    const 저장된줄 = 서버행을첨부로(서버행);
    바꾸기(planId, (c) => ({
      ...c,
      세대: c.세대 + 1,
      // 주의: 응답보다 먼저 끝난 목록 읽기가 이 파일을 이미 넣었을 수 있습니다.
      목록: c.목록.filter((f) => f.id !== 저장된줄.id).map((f) => (f.id === 임시id ? 저장된줄 : f)),
    }));
    return true;
  } catch (e) {
    바꾸기(planId, (c) => ({
      ...c,
      세대: c.세대 + 1,
      목록: c.목록.map((f) => (f.id === 임시id ? { ...f, 상태: "실패", 오류: 실패문구(e) } : f)),
    }));
    return false;
  }
}

let 다음번호 = 1;

/** 파일 하나를 올립니다. 저장에 성공하면 true. */
export function 첨부올리기(planId: string, file: File, taskId: string | null = null): Promise<boolean> {
  const id = `local-${다음번호++}`;
  const 줄: 첨부 = { id, name: file.name, size: 크기표기(file.size), taskId, 상태: "올리는중", file };
  if (!API켜짐()) {
    바꾸기(planId, (c) => ({ ...c, 세대: c.세대 + 1, 목록: [...c.목록, { ...줄, 상태: "완료" }] }));
    return Promise.resolve(true);
  }
  바꾸기(planId, (c) => ({ ...c, 세대: c.세대 + 1, 목록: [...c.목록, 줄] }));
  if (!서버계획인가(planId)) {
    바꾸기(planId, (c) => ({
      ...c,
      목록: c.목록.map((f) => (f.id === id ? { ...f, 상태: "실패", 오류: "로그인하면 저장됩니다." } : f)),
    }));
    return Promise.resolve(false);
  }
  return 줄_올리기(planId, id, file, taskId);
}

export function 첨부다시올리기(planId: string, 줄: 첨부): Promise<boolean> {
  if (!줄.file) return Promise.resolve(false);
  바꾸기(planId, (c) => ({
    ...c,
    세대: c.세대 + 1,
    목록: c.목록.map((f) => (f.id === 줄.id ? { ...f, 상태: "올리는중", 오류: undefined } : f)),
  }));
  return 줄_올리기(planId, 줄.id, 줄.file, 줄.taskId);
}

/** 서버에 저장된 파일은 지우면 되돌릴 수 없습니다. 실패한 줄은 이 탭에서만 뺍니다. 못 지우면 예외를 던집니다. */
export async function 첨부지우기(planId: string, 줄: 첨부): Promise<void> {
  if (줄.상태 === "완료" && 서버계획인가(planId)) {
    try {
      await 첨부삭제(planId, 줄.id);
    } catch (e) {
      // 이미 없는 파일은 지운 것과 같습니다.
      if ((e as { status?: number })?.status !== 404) throw e;
    }
  }
  바꾸기(planId, (c) => ({ ...c, 세대: c.세대 + 1, 목록: c.목록.filter((f) => f.id !== 줄.id) }));
}

export async function 첨부내려받기(planId: string, 줄: 첨부): Promise<void> {
  const blob = 줄.file ?? (await 첨부파일받기(planId, 줄.id));
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = 줄.name;
  link.click();
  URL.revokeObjectURL(url);
}

/** 올릴 수 없는 파일을 가려내고, 조용히 버리지 않도록 이유를 돌려줍니다. */
export function 첨부검사(현재개수: number, 파일들: File[]): { 통과: File[]; 안내: string | null } {
  const 형식오류: string[] = [];
  const 크기오류: string[] = [];
  const 후보 = 파일들.filter((f) => {
    const 확장자 = f.name.includes(".") ? f.name.split(".").pop()!.toLowerCase() : "";
    if (!첨부확장자.includes(확장자)) {
      형식오류.push(f.name);
      return false;
    }
    if (f.size > 첨부한도.바이트) {
      크기오류.push(f.name);
      return false;
    }
    if (f.size === 0) {
      크기오류.push(f.name);
      return false;
    }
    return true;
  });
  const 남은자리 = Math.max(0, 첨부한도.개수 - 현재개수);
  const 통과 = 후보.slice(0, 남은자리);
  const 안내: string[] = [];
  if (형식오류.length) {
    안내.push(`${형식오류.join(", ")}: 올릴 수 없는 형식입니다. PDF·HWP·HWPX·JPG·PNG·XLSX·DOCX 만 올릴 수 있습니다.`);
  }
  if (크기오류.length) {
    안내.push(`${크기오류.join(", ")}: 비어 있지 않은 3MB 이하 파일만 올릴 수 있습니다.`);
  }
  if (후보.length > 통과.length) {
    안내.push(`첨부파일은 지출 계획 하나에 ${첨부한도.개수}개까지 올릴 수 있어 ${후보.length - 통과.length}개는 올리지 않았습니다.`);
  }
  return { 통과, 안내: 안내.length ? 안내.join(" ") : null };
}
