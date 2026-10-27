FROM golang:1.26-alpine AS builder
WORKDIR /app
COPY . .
RUN CGO_ENABLED=0 go build -o aperture .

FROM alpine:latest
WORKDIR /root/
COPY --from=builder /app/aperture /usr/local/bin/aperture
EXPOSE 8080 8081
ENTRYPOINT ["aperture"]
