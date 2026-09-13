/**
 * The browser half of WebAuthn ceremonies — IAM-010/IAM-012. No
 * `@simplewebauthn/browser` or equivalent is installed anywhere in this
 * repo, so this is the minimal, hand-written adapter between what
 * `api.auth.routes`' `/mfa/passkey/*` and `/login/passkey/*` endpoints send
 * as `options` (the exact JSON shape `webauthn.helpers.options_to_json_dict`
 * produces server-side - base64url strings for every byte field) and what
 * `navigator.credentials.create()`/`.get()` need (real `ArrayBuffer`s), and
 * back again into the JSON shape
 * `webauthn.helpers.parse_registration_credential_json`/
 * `parse_authentication_credential_json` expect on the way in.
 *
 * Deliberately thin: no retry, no platform-detection, no conditional-UI
 * autofill wiring. A person clicks a button, the browser's own WebAuthn UI
 * takes over, and this module's only job is the byte-format translation on
 * both sides of that.
 */

export class PasskeyUnavailableError extends Error {}

/** A person dismissed the prompt, or the platform refused it (e.g. no
 * matching credential for an authentication ceremony). Distinct from
 * `PasskeyUnavailableError` (the API does not exist in this browser at all)
 * because the two call for different messages. */
export class PasskeyCeremonyError extends Error {
  constructor(cause: unknown) {
    super(cause instanceof Error ? cause.message : "WebAuthn ceremony failed");
  }
}

/** Returns a plain `ArrayBuffer` rather than a `Uint8Array` view over one -
 * `Uint8Array`'s own `.buffer` type widened to `ArrayBufferLike` in newer
 * `lib.dom.d.ts` versions, which no longer satisfies `BufferSource` at every
 * WebAuthn option field below without an unsound cast. An `ArrayBuffer` is
 * itself a valid `BufferSource` and sidesteps the mismatch entirely. */
function base64UrlToBytes(value: string): ArrayBuffer {
  const padded = value
    .replace(/-/g, "+")
    .replace(/_/g, "/")
    .padEnd(Math.ceil(value.length / 4) * 4, "=");
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

function bytesToBase64Url(bytes: ArrayBuffer): string {
  let binary = "";
  for (const byte of new Uint8Array(bytes)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function requireWebAuthn(): void {
  if (
    typeof navigator === "undefined" ||
    !navigator.credentials ||
    !("PublicKeyCredential" in window)
  ) {
    throw new PasskeyUnavailableError("This browser does not support passkeys.");
  }
}

interface CredentialDescriptorJson {
  id: string;
  type: string;
  transports?: string[];
}

interface CreationOptionsJson {
  rp: { id?: string; name: string };
  user: { id: string; name: string; displayName: string };
  challenge: string;
  pubKeyCredParams: Array<{ type: string; alg: number }>;
  timeout?: number;
  excludeCredentials?: CredentialDescriptorJson[];
  authenticatorSelection?: {
    authenticatorAttachment?: string;
    residentKey?: string;
    requireResidentKey?: boolean;
    userVerification?: string;
  };
  attestation?: string;
}

interface RequestOptionsJson {
  challenge: string;
  timeout?: number;
  rpId?: string;
  allowCredentials?: CredentialDescriptorJson[];
  userVerification?: string;
}

/** `mfa_passkey_enroll_begin`/`login_passkey_begin`'s `options` field,
 * parsed and handed to `navigator.credentials.create()`, then serialized
 * back into the JSON body `mfa_passkey_enroll_finish`/`login_passkey_finish`
 * expect as `credential`. */
export async function createPasskey(
  optionsJson: string,
  onFriendlyName?: (defaultName: string) => string,
): Promise<{ credential: Record<string, unknown>; suggestedName: string }> {
  requireWebAuthn();
  const options = JSON.parse(optionsJson) as CreationOptionsJson;

  const publicKey: PublicKeyCredentialCreationOptions = {
    rp: options.rp,
    user: {
      id: base64UrlToBytes(options.user.id),
      name: options.user.name,
      displayName: options.user.displayName,
    },
    challenge: base64UrlToBytes(options.challenge),
    pubKeyCredParams: options.pubKeyCredParams as PublicKeyCredentialParameters[],
    timeout: options.timeout,
    excludeCredentials: options.excludeCredentials?.map((cred) => ({
      id: base64UrlToBytes(cred.id),
      type: "public-key",
      transports: cred.transports as AuthenticatorTransport[] | undefined,
    })),
    authenticatorSelection: options.authenticatorSelection as
      AuthenticatorSelectionCriteria | undefined,
    attestation: options.attestation as AttestationConveyancePreference | undefined,
  };

  let credential: Credential | null;
  try {
    credential = await navigator.credentials.create({ publicKey });
  } catch (cause) {
    throw new PasskeyCeremonyError(cause);
  }
  if (!credential || !("rawId" in credential)) {
    throw new PasskeyCeremonyError(new Error("no credential returned"));
  }
  const publicKeyCredential = credential as PublicKeyCredential;
  const response = publicKeyCredential.response as AuthenticatorAttestationResponse;

  const suggestedName = onFriendlyName ? onFriendlyName(defaultDeviceName()) : defaultDeviceName();

  return {
    suggestedName,
    credential: {
      id: publicKeyCredential.id,
      rawId: bytesToBase64Url(publicKeyCredential.rawId),
      type: "public-key",
      response: {
        clientDataJSON: bytesToBase64Url(response.clientDataJSON),
        attestationObject: bytesToBase64Url(response.attestationObject),
      },
    },
  };
}

/** `mfa_passkey_verify_begin`/`login_passkey_begin`'s authentication twin. */
export async function getPasskey(optionsJson: string): Promise<Record<string, unknown>> {
  requireWebAuthn();
  const options = JSON.parse(optionsJson) as RequestOptionsJson;

  const publicKey: PublicKeyCredentialRequestOptions = {
    challenge: base64UrlToBytes(options.challenge),
    timeout: options.timeout,
    rpId: options.rpId,
    allowCredentials: options.allowCredentials?.map((cred) => ({
      id: base64UrlToBytes(cred.id),
      type: "public-key",
      transports: cred.transports as AuthenticatorTransport[] | undefined,
    })),
    userVerification: options.userVerification as UserVerificationRequirement | undefined,
  };

  let credential: Credential | null;
  try {
    credential = await navigator.credentials.get({ publicKey });
  } catch (cause) {
    throw new PasskeyCeremonyError(cause);
  }
  if (!credential || !("rawId" in credential)) {
    throw new PasskeyCeremonyError(new Error("no credential returned"));
  }
  const publicKeyCredential = credential as PublicKeyCredential;
  const response = publicKeyCredential.response as AuthenticatorAssertionResponse;

  return {
    id: publicKeyCredential.id,
    rawId: bytesToBase64Url(publicKeyCredential.rawId),
    type: "public-key",
    response: {
      clientDataJSON: bytesToBase64Url(response.clientDataJSON),
      authenticatorData: bytesToBase64Url(response.authenticatorData),
      signature: bytesToBase64Url(response.signature),
    },
  };
}

function defaultDeviceName(): string {
  const ua = typeof navigator === "undefined" ? "" : navigator.userAgent;
  if (/iphone|ipad/i.test(ua)) return "iPhone or iPad";
  if (/android/i.test(ua)) return "Android device";
  if (/mac os/i.test(ua)) return "Mac";
  if (/windows/i.test(ua)) return "Windows device";
  return "Passkey";
}
