package in.cnxy.connector;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication @EnableScheduling
public class ConnectorApplication { public static void main(String[] args) { SpringApplication.run(ConnectorApplication.class, args); } }
