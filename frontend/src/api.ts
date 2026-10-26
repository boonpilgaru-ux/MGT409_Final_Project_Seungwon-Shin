// Typed fetch wrappers for every /api endpoint (SPEC section 4). All functions throw ApiError on failure.
import type {
  Answer,
  AnswerResult,
  ContextResponse,
  ContextUpload,
  Country,
  CreateOrderRequest,
  Customer,
  DesignGenerateRequest,
  DesignResponse,
  Health,
  Order,
  Price,
  ProductSpec,
  Profile,
  Question,
  SessionState,
  ShopResponse,
  UsageSummary,
} from "./types";
import { getOrderToken, setOrderToken } from "./session";

const BASE = "/api";

/** HTTP error with the backend's `detail` message (status 0 = network failure / server unreachable). */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }
  get isConflict(): boolean {
    return this.status === 409;
  }
  /** Upstream AI / web failure (502) — usually worth a "Try again" button. */
  get isUpstream(): boolean {
    return this.status === 502 || this.status === 504;
  }
}

/** Human-friendly message for any thrown value (ApiError, AbortError, unknown). */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof DOMException && err.name === "AbortError") return "Request cancelled.";
  if (err instanceof Error) return err.message;
  return "Something went wrong.";
}

export function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

function detailFrom(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (Array.isArray(detail)) {
      const msgs = detail.map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : String(d)));
      if (msgs.length) return msgs.join("; ");
    }
  }
  return fallback;
}

const STATUS_FALLBACK: Record<number, string> = {
  403: "You can't change that.",
  404: "Not found.",
  409: "That step isn't available yet.",
  411: "That upload couldn't be sent. Please try again.",
  413: "That upload is too large.",
  422: "Some of that input isn't valid.",
  429: "You're going a little fast. Please wait a moment and try again.",
  500: "Something went wrong on our side.",
  501: "This feature isn't ready yet.",
  502: "Our AI had a hiccup. Please try again.",
  504: "The server took too long. Please try again.",
};

interface RequestOptions {
  /** Extra request headers. */
  headers?: Record<string, string>;
  /** Sees the response headers of a successful call. */
  onHeaders?: (headers: Headers) => void;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  signal?: AbortSignal,
  options: RequestOptions = {},
): Promise<T> {
  const init: RequestInit = { method, signal, headers: { Accept: "application/json", ...options.headers } };
  if (body instanceof FormData) {
    init.body = body; // browser sets the multipart boundary
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }

  let res: Response;
  try {
    res = await fetch(BASE + path, init);
  } catch (err) {
    if (isAbortError(err)) throw err;
    throw new ApiError(0, "Can't reach the Tee:dentity server. Is the backend running?");
  }

  const text = await res.text();
  let data: unknown = undefined;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = undefined;
    }
  }
  if (!res.ok) {
    throw new ApiError(res.status, detailFrom(data, STATUS_FALLBACK[res.status] ?? `Request failed (${res.status}).`));
  }
  options.onHeaders?.(res.headers);
  return data as T;
}

const enc = encodeURIComponent;

// ---------------------------------------------------------------- health & sessions
export const getHealth = (signal?: AbortSignal) => request<Health>("GET", "/health", undefined, signal);

export const createSession = (country: Country, signal?: AbortSignal) =>
  request<SessionState>("POST", "/session", { country }, signal);

export const getSession = (sessionId: string, signal?: AbortSignal) =>
  request<SessionState>("GET", `/session/${enc(sessionId)}`, undefined, signal);

export const getUsage = (sessionId: string, signal?: AbortSignal) =>
  request<UsageSummary>("GET", `/usage/${enc(sessionId)}`, undefined, signal);

/** Extra endpoint: delete the session on the server (answers, uploads' text, profile, designs). Orders stay. */
export const deleteSession = (sessionId: string, signal?: AbortSignal) =>
  request<void>("DELETE", `/session/${enc(sessionId)}`, undefined, signal);

// ---------------------------------------------------------------- balance game
/** Extra endpoint: pre-generate round 1 for a country so the game opens instantly. Fire-and-forget (202). */
export const warmQuiz = (country: Country, signal?: AbortSignal) =>
  request<void>("POST", "/quiz/warm", { country }, signal);

/** Idempotent: returns the current question if the game already started. */
export const startQuiz = (sessionId: string, signal?: AbortSignal) =>
  request<Question>("POST", `/quiz/${enc(sessionId)}/start`, undefined, signal);

/** 409 if `answer.question_id` isn't the current question (e.g. double submit). */
export const answerQuiz = (sessionId: string, answer: Answer, signal?: AbortSignal) =>
  request<AnswerResult>("POST", `/quiz/${enc(sessionId)}/answer`, answer, signal);

// ---------------------------------------------------------------- about you
/** Multipart upload; returns ALL context items for the session. */
export function addContext(sessionId: string, upload: ContextUpload, signal?: AbortSignal) {
  const form = new FormData();
  for (const file of upload.files ?? []) form.append("files", file, file.name);
  if (upload.urls?.length) form.append("urls", JSON.stringify(upload.urls));
  if (upload.text?.trim()) form.append("text", upload.text.trim());
  return request<ContextResponse>("POST", `/context/${enc(sessionId)}`, form, signal);
}

export const removeContext = (sessionId: string, itemId: string, signal?: AbortSignal) =>
  request<ContextResponse>("DELETE", `/context/${enc(sessionId)}/${enc(itemId)}`, undefined, signal);

// ---------------------------------------------------------------- profile, shop, studio
/** Regenerates on every call. 409 if the quiz isn't finished. */
export const buildProfile = (sessionId: string, signal?: AbortSignal) =>
  request<Profile>("POST", `/profile/${enc(sessionId)}`, undefined, signal);

/** Live web search; can take up to ~75 s. 409 if there's no profile yet. */
export const suggestProducts = (sessionId: string, signal?: AbortSignal) =>
  request<ShopResponse>("POST", `/shop/${enc(sessionId)}/suggest`, undefined, signal);

/** Returns only the NEW designs; SessionState.designs holds all of them. */
export const generateDesigns = (sessionId: string, req: DesignGenerateRequest, signal?: AbortSignal) =>
  request<DesignResponse>("POST", `/design/${enc(sessionId)}/generate`, req, signal);

// ---------------------------------------------------------------- orders
/** Extra endpoint (not in the SPEC table): price estimate for a spec, for the Studio. */
export const quotePrice = (spec: ProductSpec, signal?: AbortSignal) =>
  request<Price>("POST", "/quote", spec, signal);

export const createOrder = (req: CreateOrderRequest, signal?: AbortSignal) =>
  request<Order>("POST", "/orders", req, signal);

export const getOrder = (orderId: string, signal?: AbortSignal) =>
  request<Order>("GET", `/orders/${enc(orderId)}`, undefined, signal);

/**
 * Demo checkout: records the customer and sets status "requested". No payment.
 * The first request returns an X-Order-Token that we keep in this browser; only that token can
 * update an already-requested order (403 otherwise).
 */
export function requestOrder(orderId: string, customer: Customer, signal?: AbortSignal) {
  const token = getOrderToken(orderId);
  return request<Order>("POST", `/orders/${enc(orderId)}/request`, customer, signal, {
    headers: token ? { "X-Order-Token": token } : undefined,
    onHeaders: (headers) => {
      const issued = headers.get("X-Order-Token");
      if (issued) setOrderToken(orderId, issued);
    },
  });
}

/** True if this browser placed the request for `orderId` (and may update it). */
export const isOrderRequester = (orderId: string) => Boolean(getOrderToken(orderId));

/** URL of the downloadable print-ready SVG (use as an <a href download>). */
export const printSvgUrl = (orderId: string) => `${BASE}/orders/${enc(orderId)}/print.svg`;

/** Safe way to display a design: `<img src={svgDataUrl(design.svg)} alt=... />` (never innerHTML). */
export const svgDataUrl = (svg: string) => `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
