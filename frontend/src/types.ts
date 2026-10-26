// TypeScript mirror of backend/app/models.py (SPEC section 4). Field names are EXACT — change both together.

// ---------------------------------------------------------------- shared literals
export type Depth = "broad" | "focused" | "specific";
export type Choice = "A" | "B";
export type ContextKind = "file" | "url" | "text";
export type ContextStatus = "ok" | "limited" | "error";
export type Origin = "local" | "global" | "indie";
export type PickMatchSource = "product_page" | "search_result";
export type Fit = "regular" | "oversized" | "slim" | "boxy" | "cropped";
export type Material =
  | "cotton"
  | "organic_cotton"
  | "heavyweight_cotton"
  | "tri_blend"
  | "linen_blend"
  | "recycled_poly";
export type Neckline = "crew" | "v_neck" | "mock_neck";
export type Sleeve = "short" | "long" | "sleeveless";
export type Size = "XS" | "S" | "M" | "L" | "XL" | "XXL";
export type PrintMethod = "dtg" | "screen" | "embroidery" | "puff";
export type Placement = "front_center" | "left_chest" | "back" | "front_and_back";
export type OrderStatus = "draft" | "requested";

export const QUIZ_TOTAL = 10;

// ---------------------------------------------------------------- section 4 models
export interface Country {
  code: string; // ISO 3166-1 alpha-2, uppercase
  name: string;
}

export interface OptionImage {
  url: string;
  credit: string;
  source_url: string | null;
}

export interface Option {
  label: string;
  sublabel: string | null;
  emoji: string;
  color: string; // hex, e.g. "#ff3b30"
  image: OptionImage | null;
}

export interface Question {
  id: string;
  index: number; // 1-based
  total: number; // 10
  prompt: string;
  depth: Depth;
  option_a: Option;
  option_b: Option;
}

export interface Answer {
  question_id: string;
  choice: Choice;
}

export interface ContextItem {
  id: string;
  kind: ContextKind;
  name: string;
  chars: number;
  status: ContextStatus;
  summary: string;
  error: string | null;
}

export interface Trait {
  name: string;
  score: number; // 0-100
  low_label: string;
  high_label: string;
  description: string;
}

export interface TasteColor {
  hex: string;
  name: string;
}

export interface Taste {
  colors: TasteColor[]; // 4-6
  aesthetics: string[];
  motifs: string[];
  typography: string;
  fits: string[];
  avoid: string[];
}

export interface Profile {
  archetype: string;
  tagline: string;
  summary: string;
  traits: Trait[]; // 5-7
  taste: Taste;
  cultural_roots: string;
  values: string[];
  tshirt_brief: string;
}

export interface Product {
  id: string;
  title: string;
  brand: string;
  retailer: string;
  url: string;
  image_url: string | null;
  price: string | null;
  currency: string | null;
  origin: Origin;
  why: string;
  pick_match: string | null;
  pick_match_source: PickMatchSource | null;
}

export interface ProductSpec {
  shirt_color: string; // hex
  fit: Fit;
  material: Material;
  neckline: Neckline;
  sleeve: Sleeve;
  size: Size;
  print_method: PrintMethod;
  placement: Placement;
  quantity: number; // 1-50
  notes: string; // <= 500 chars
}

export interface Design {
  id: string;
  title: string;
  concept: string;
  svg: string; // sanitized standalone SVG, viewBox "0 0 1000 1000"
  palette: string[];
  recommended_spec: ProductSpec;
}

export interface PriceLine {
  label: string;
  amount: number;
}

export interface Price {
  unit: number;
  total: number;
  currency: "USD";
  breakdown: PriceLine[];
}

export interface Customer {
  name: string;
  email: string;
  address: string;
}

export interface Order {
  id: string;
  created_at: string; // ISO 8601
  status: OrderStatus;
  design: Design;
  spec: ProductSpec;
  price: Price;
  profile_archetype: string | null;
  country: Country | null;
  customer: Customer | null;
}

export interface AnsweredQuestion {
  question: Question;
  choice: Choice;
}

export interface UsageSummary {
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  request_count: number;
  estimated_cost_usd: number;
}

export interface SessionState {
  id: string;
  country: Country;
  answers: AnsweredQuestion[];
  current_question: Question | null;
  quiz_done: boolean;
  context: ContextItem[];
  profile: Profile | null;
  designs: Design[];
}

// ---------------------------------------------------------------- request / response bodies
export interface Health {
  ok: boolean;
  portkey_key_set: boolean;
  models: Record<string, string>;
}

export interface CreateSessionRequest {
  country: Country;
}

export interface AnswerResult {
  next: Question | null;
  done: boolean;
}

export interface ContextResponse {
  items: ContextItem[];
}

export interface ContextUpload {
  files?: File[]; // 0-5 per session; pdf, docx, txt, md; <= 10 MB each
  urls?: string[]; // <= 5 per session
  text?: string | null;
}

export interface ShopResponse {
  products: Product[];
}

export interface DesignGenerateRequest {
  feedback: string | null;
  base_design_id: string | null;
  count: number; // 1-3
}

export interface DesignResponse {
  designs: Design[];
}

export interface CreateOrderRequest {
  session_id: string;
  design_id: string;
  spec: ProductSpec;
}

/** Same defaults the backend uses for ProductSpec. */
export const DEFAULT_SPEC: ProductSpec = {
  shirt_color: "#ffffff",
  fit: "regular",
  material: "cotton",
  neckline: "crew",
  sleeve: "short",
  size: "M",
  print_method: "dtg",
  placement: "front_center",
  quantity: 1,
  notes: "",
};
