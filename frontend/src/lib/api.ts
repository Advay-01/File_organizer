function statusLabel(response: Response) {
  return `HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""}`;
}

export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

function errorDetail(payload: unknown): string | null {
  if (!payload || typeof payload !== "object" || !("detail" in payload)) return null;
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object" && "msg" in item && typeof item.msg === "string") return item.msg;
        return "";
      })
      .filter(Boolean);
    return messages.length ? messages.join("; ") : null;
  }
  return null;
}

export async function parseJsonResponse<T>(
  response: Response,
  fallbackMessage: string,
): Promise<T> {
  const text = await response.text();
  let payload: unknown;
  let hasJson = false;

  if (text.trim()) {
    try {
      payload = JSON.parse(text);
      hasJson = true;
    } catch {
      // Never surface an HTML error page or malformed body as request output.
    }
  }

  if (!response.ok) {
    const detail = hasJson ? errorDetail(payload) : null;
    throw new ApiError(
      detail ?? `${fallbackMessage} (${statusLabel(response)})`,
      response.status,
      payload,
    );
  }

  if (!hasJson) {
    throw new Error(`${fallbackMessage} The server returned no readable JSON response (${statusLabel(response)}).`);
  }

  return payload as T;
}

export async function fetchJson<T>(
  url: string,
  init: RequestInit | undefined,
  fallbackMessage: string,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch {
    throw new Error(
      "Could not reach the local Sortly service. Check History before trying again; do not repeat the Apply operation.",
    );
  }
  return parseJsonResponse<T>(response, fallbackMessage);
}
