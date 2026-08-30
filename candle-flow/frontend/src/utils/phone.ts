export function maskPhone(phone: string) {
  if (phone.length === 11) return `${phone.slice(0, 3)}****${phone.slice(7)}`
  return phone
}
